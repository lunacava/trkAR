"""trkAR の LiDAR 深度 (NDI "trkAR-Depth") を受けて、カメラの前に立体スクリーンを作る。

受け方は 2 通り:
    UDP (既定): trkAR が SystemPC IP の 9100 番へ無圧縮で送る "ARD1" パケット
        (UDPDepthSender.swift)。NDI の圧縮による誤差が乗らないのでこちらが綺麗。
    NDI: FourCC PA16。Y プレーン = 深度 (UInt16 × scale = メートル、0 = 無効)、
    Alpha プレーン = 信頼度 (0 / 32768 / 65535)。
    メタデータ <trkARDepth w h fx fy cx cy scale m00..m33/> の fx..cy は深度解像度基準。

深度画像は 4:3 の全体、カラーの NDI は横幅そのままで上下を切った 16:9 なので、
カラーと重なる中央 3/4 の行だけを使う。各画素を
    X = (u − cx) / fx · d,  Y = −(v − cy) / fy · d,  Z = −d
でカメラ座標 (−Z 前方 / Y 上。Blender のカメラと同じ) に逆投影した格子メッシュを
追従カメラの子にすると、カメラから見て実写とぴったり重なる。
信頼度の低い画素・遠すぎる画素・前後の段差の境目は、画素単位のマスク画像
`trkAR Depth Mask` で透明にする。マスクはぼかしてから線形補間で読み、しきい値
(エッジ位置) とぼかし幅をマテリアルで決めるので、格子の粗さに関係なく輪郭が滑らかになる。
面属性 `trkar_valid` (面の 4 隅が有効か) も書いておくが、マテリアルでは使っていない。
"""

import collections
import ctypes
import re
import socket
import struct
import threading
import time

import bpy
import numpy as np

from . import ndi, ndi_input

OBJECT_NAME = "trkAR Depth Screen"
# 人の後ろに置く実写の板 (深度スクリーンの子)。人の居ない所はこれが見える
BACKGROUND_NAME = "trkAR Depth Background"
MASK_IMAGE_NAME = "trkAR Depth Mask"
MASK_NODE = "trkAR Mask"
EDGE_NODE = "trkAR Edge"
ALPHA_NODE = "trkAR Video Alpha"
# 映像のアルファ (RVM) で切るとき、ARKit の人物マスクより外側まで人の深度を広げる画素数
# (RVM の輪郭や髪は ARKit のマスクより外へ出るので、そこを背景の深度にしない)
MATTE_GROW = 3
# マスクのぼかし回数 ([1 2 1] を縦横に)。エッジを動かせる幅もこれで決まる (±3 画素ほど)
MASK_BLUR_PASSES = 3
VALID_ATTRIBUTE = "trkar_valid"
# trkAR のカラーは 16:9 (縦位置なら 9:16)。4:3 のセンサー画像から中央を切り出している
VIDEO_ASPECT = 16.0 / 9.0

UDP_DEFAULT_PORT = 9100
# "ARD1": bit 0-13 = 深度 mm。"ARD2": bit 0-12 = 深度 mm、bit 13 = 人物 (personSegmentation)
# "ARD3": ARD2 + ヘッダーの後ろに timecode (Int64、100ns、実写 NDI の timecode と同じ時計)
UDP_MAGICS = (b"ARD1", b"ARD2", b"ARD3")
# 人の画素なのに近くの人の画素よりこれ以上奥にある深度は、背景のにじみとして置き換える (m)
PERSON_BLEED = 0.3
# magic, frame_id, width, height, row_start, row_count, packet_index, packet_count, fx, fy, cx, cy, reserved
UDP_HEADER = struct.Struct("<4sIHHHHHH4fI")
UDP_TIMECODE = struct.Struct("<q")
# 映像と揃えるために取っておく処理済みの深度 (30fps で 1 秒分)
SYNC_QUEUE = 30
# 深度と映像の時刻がこれ以上離れていたら時計が違う (古い trkAR など) とみなして揃えない (100ns)
SYNC_LIMIT = 20_000_000

_ATTR = re.compile(r'(\w+)="([^"]*)"')


def parse_metadata(text):
    # trkAR 1.1.1 までは <ARtrkDepth …/> (名前を変える前)
    if not text or ("trkARDepth" not in text and "ARtrkDepth" not in text):
        return None
    values = {}
    for key, value in _ATTR.findall(text):
        try:
            values[key] = float(value)
        except ValueError:
            pass
    if not all(k in values for k in ("fx", "fy", "cx", "cy")):
        return None
    return values


class Grid:
    """深度画像のどの画素を頂点にするか。topology が同じなら使い回す。"""

    def __init__(self, width, height, step):
        self.width = width
        self.height = height
        self.step = step
        # カラーと重なる範囲 (割合)。横長の深度は上下を、縦長 (縦位置) の深度は左右を切る
        target = VIDEO_ASPECT if width >= height else 1.0 / VIDEO_ASPECT
        ratio = width / height
        crop_y = 0.5 * (1.0 - ratio / target) if ratio < target else 0.0
        crop_x = 0.5 * (1.0 - target / ratio) if ratio > target else 0.0
        self.crop = (crop_x, crop_y)
        self.top = int(np.ceil(height * crop_y - 0.5))
        self.bottom = int(np.floor(height * (1.0 - crop_y) - 0.5))
        self.left = int(np.ceil(width * crop_x - 0.5))
        self.right = int(np.floor(width * (1.0 - crop_x) - 0.5))
        self.rows = np.arange(self.top, self.bottom + 1, step)
        self.cols = np.arange(self.left, self.right + 1, step)
        # カラー画像上の UV (Blender は下が 0)
        v_top = ((self.rows + 0.5) / height - crop_y) / (1.0 - 2.0 * crop_y)
        u = ((self.cols + 0.5) / width - crop_x) / (1.0 - 2.0 * crop_x)
        uu, vv = np.meshgrid(u, 1.0 - v_top)
        self.uv = np.stack([uu, vv], axis=-1).reshape(-1, 2).astype(np.float32)

    @property
    def band(self):
        """カラーと重なる画素の範囲 (行, 列)。"""
        return slice(self.top, self.bottom + 1), slice(self.left, self.right + 1)

    @property
    def key(self):
        return (self.width, self.height, self.step)

    @property
    def shape(self):
        return len(self.rows), len(self.cols)

    def faces(self):
        rows, cols = self.shape
        index = np.arange(rows * cols).reshape(rows, cols)
        a = index[:-1, :-1]
        b = index[1:, :-1]
        c = index[1:, 1:]
        d = index[:-1, 1:]
        # 左上 → 左下 → 右下 → 右上。カメラ側 (+Z) を向く
        return np.stack([a, b, c, d], axis=-1).reshape(-1, 4)


def build_mesh(mesh, grid):
    rows, cols = grid.shape
    mesh.clear_geometry()
    faces = grid.faces()
    verts = np.zeros((rows * cols, 3), dtype=np.float32)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", verts.ravel())
    mesh.loops.add(faces.size)
    mesh.loops.foreach_set("vertex_index", faces.ravel().astype(np.int32))
    mesh.polygons.add(len(faces))
    mesh.polygons.foreach_set("loop_start", (np.arange(len(faces)) * 4).astype(np.int32))
    mesh.update(calc_edges=True)
    uv = mesh.uv_layers.new(name="UVMap") if not mesh.uv_layers else mesh.uv_layers[0]
    uv.data.foreach_set("uv", grid.uv[faces.ravel()].ravel())
    if VALID_ATTRIBUTE in mesh.attributes:
        mesh.attributes.remove(mesh.attributes[VALID_ATTRIBUTE])
    mesh.attributes.new(VALID_ATTRIBUTE, "FLOAT", "FACE")
    mesh["trkar_grid"] = list(grid.key)


def ensure_background(screen):
    """深度スクリーンの子に、実写をそのまま (アルファなし) 貼った板を用意する。"""
    obj = bpy.data.objects.get(BACKGROUND_NAME)
    if obj is None or obj.type != "MESH":
        mesh = bpy.data.meshes.new(BACKGROUND_NAME)
        mesh.from_pydata([(0, 0, 0)] * 4, [], [(0, 1, 2, 3)])
        uv = mesh.uv_layers.new(name="UVMap")
        # 左上 → 左下 → 右下 → 右上 (深度スクリーンの面と同じ順。カメラ側を向く)
        for loop, coord in zip(uv.data, ((0, 1), (0, 0), (1, 0), (1, 1))):
            loop.uv = coord
        material = bpy.data.materials.new(BACKGROUND_NAME)
        material.use_nodes = True
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        nodes.clear()
        tex = nodes.new("ShaderNodeTexImage")
        tex.image = ndi_input.ensure_image()
        tex.extension = "EXTEND"
        tex.location = (-400, 0)
        emission = nodes.new("ShaderNodeEmission")
        emission.location = (-100, 0)
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (150, 0)
        links.new(tex.outputs["Color"], emission.inputs["Color"])
        links.new(emission.outputs["Emission"], output.inputs["Surface"])
        mesh.materials.append(material)
        obj = bpy.data.objects.new(BACKGROUND_NAME, mesh)
        if hasattr(obj, "visible_shadow"):
            obj.visible_shadow = False
        collection = screen.users_collection[0] if screen.users_collection else bpy.context.scene.collection
        collection.objects.link(obj)
    if obj.parent is not screen:
        obj.parent = screen
        obj.matrix_parent_inverse.identity()
        obj.matrix_basis.identity()
    return obj


def place_background(obj, grid, meta, distance):
    """板を distance (カメラ座標、m) に、実写の範囲 (UV 0..1) ちょうどの大きさで置く。
    深度スクリーンと同じ逆投影なので、カメラから見ると実写と重なる。"""
    crop_x, crop_y = grid.crop
    # UV 0 / 1 に当たる画素の位置 (頂点は画素の番号で逆投影している。中心 = 番号 + 0.5)
    left = crop_x * grid.width - 0.5
    right = (1.0 - crop_x) * grid.width - 0.5
    top = crop_y * grid.height - 0.5
    bottom = (1.0 - crop_y) * grid.height - 0.5
    def corner(u, v):
        return ((u - meta["cx"]) / meta["fx"] * distance, -(v - meta["cy"]) / meta["fy"] * distance, -distance)
    co = np.array([corner(left, top), corner(left, bottom), corner(right, bottom), corner(right, top)],
                  dtype=np.float32)
    obj.data.vertices.foreach_set("co", co.ravel())
    obj.data.update()


def ensure_mask_image(width=16, height=9):
    image = bpy.data.images.get(MASK_IMAGE_NAME)
    if image is None:
        image = bpy.data.images.new(MASK_IMAGE_NAME, width, height, alpha=False)
        image.generated_color = (0.0, 0.0, 0.0, 1.0)
        image.colorspace_settings.name = "Non-Color"
    return image


def make_material():
    material = bpy.data.materials.new(OBJECT_NAME)
    build_material(material)
    return material


def build_material(material):
    material.use_nodes = True
    if hasattr(material, "surface_render_method"):
        material.surface_render_method = "DITHERED"
    elif hasattr(material, "blend_method"):
        material.blend_method = "CLIP"
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    nodes.clear()
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = ndi_input.ensure_image()
    tex.extension = "EXTEND"
    tex.location = (-500, -150)
    # ソリッド表示の「テクスチャ」はアクティブな画像ノードを見るので、実写の方にしておく
    nodes.active = tex
    emission = nodes.new("ShaderNodeEmission")
    emission.location = (-200, -150)
    transparent = nodes.new("ShaderNodeBsdfTransparent")
    transparent.location = (-200, 0)
    mask = nodes.new("ShaderNodeTexImage")
    mask.name = MASK_NODE
    mask.image = ensure_mask_image()
    mask.interpolation = "Linear"
    mask.extension = "EXTEND"
    mask.location = (-700, 250)
    edge = nodes.new("ShaderNodeMapRange")
    edge.name = EDGE_NODE
    edge.interpolation_type = "SMOOTHSTEP"
    edge.clamp = True
    edge.location = (-400, 250)
    # 実写にアルファがあれば (RVM で切り抜いた "trkAR Matte" など) それも掛ける。不透明な映像なら 1
    alpha = nodes.new("ShaderNodeMath")
    alpha.name = ALPHA_NODE
    alpha.operation = "MULTIPLY"
    alpha.use_clamp = True
    alpha.location = (-150, 250)
    mix = nodes.new("ShaderNodeMixShader")
    mix.location = (50, 50)
    output = nodes.new("ShaderNodeOutputMaterial")
    output.location = (250, 50)
    links.new(tex.outputs["Color"], emission.inputs["Color"])
    links.new(mask.outputs["Color"], edge.inputs["Value"])
    links.new(edge.outputs["Result"], alpha.inputs[0])
    links.new(tex.outputs["Alpha"], alpha.inputs[1])
    links.new(alpha.outputs["Value"], mix.inputs["Fac"])
    links.new(transparent.outputs["BSDF"], mix.inputs[1])
    links.new(emission.outputs["Emission"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output.inputs["Surface"])


def ensure_material(obj):
    """古い (面属性で切り抜く) マテリアルなら作り直す。"""
    mesh = obj.data
    if not mesh.materials or mesh.materials[0] is None:
        if mesh.materials:
            mesh.materials[0] = make_material()
        else:
            mesh.materials.append(make_material())
    material = mesh.materials[0]
    if material.node_tree is None or ALPHA_NODE not in material.node_tree.nodes:
        build_material(material)
    nodes = material.node_tree.nodes
    if nodes.active is None or nodes.active.name == MASK_NODE:
        tex = next((n for n in nodes if n.type == "TEX_IMAGE" and n.name != MASK_NODE), None)
        if tex is not None:
            nodes.active = tex
    return material


def update_edge(settings):
    """エッジ位置 / ぼかしをマテリアルに反映する。"""
    obj = bpy.data.objects.get(OBJECT_NAME)
    if obj is None or obj.type != "MESH":
        return
    material = ensure_material(obj)
    edge = material.node_tree.nodes.get(EDGE_NODE)
    if edge is None:
        return
    # マスクはぼかしてあるので、しきい値を下げると輪郭が外へ、上げると内へ動く
    # 範囲を 0..1 の内側に収める (外に出ると全面が半透明 / 不透明になる)
    threshold = 0.5 - 0.4 * settings.depth_edge_shift
    half = max(0.005, 0.5 * settings.depth_edge_feather)
    edge.inputs["From Min"].default_value = max(0.01, threshold - half)
    edge.inputs["From Max"].default_value = min(0.99, threshold + half)


def _shifted(a, dy, dx):
    """端をのばして 1 画素ずらした配列 (近傍の比較用)。"""
    padded = np.pad(a, 1, mode="edge")
    h, w = a.shape
    return padded[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]


def _spread_min(a):
    return np.minimum.reduce([a, _shifted(a, 0, 1), _shifted(a, 0, -1), _shifted(a, 1, 0), _shifted(a, -1, 0)])


def _dilate(a, passes):
    for _ in range(passes):
        a = a | _shifted(a, 0, 1) | _shifted(a, 0, -1) | _shifted(a, 1, 0) | _shifted(a, -1, 0)
    return a


def _blur(a, passes):
    for _ in range(passes):
        a = 0.25 * _shifted(a, 0, -1) + 0.5 * a + 0.25 * _shifted(a, 0, 1)
        a = 0.25 * _shifted(a, -1, 0) + 0.5 * a + 0.25 * _shifted(a, 1, 0)
    return a


class DepthInput:
    def __init__(self):
        self.receiver = None
        self.source_name = ""
        self.frames_received = 0
        self.frames_shown = 0
        self.resolution = (0, 0)
        self.valid_ratio = 0.0
        self.last_error = ""
        # 受信スレッドが読むパラメーター (メインスレッドの tick で設定から写す)
        self.step = 2
        self.max_distance = 5.0
        self.min_confidence = 1
        self.edge_tolerance = 0.1
        self.denoise = True
        self.temporal = 0.5
        self.people = "EDGE"
        self.has_people = False
        self.person_ratio = 0.0
        # 深度の取れない機種 (iPad mini など) から人物マスクだけが来ている
        self.person_only = False
        # 実写にアルファがある (RVM の切り抜き)。輪郭はそちらに任せる
        self.video_alpha = False
        # 深度メッシュを使わず、ARKit の人物マスクを実写 NDI 映像のアルファに直接使う
        # (RVM が重すぎる Windows などで、深度を無視した平らなカットアウトが欲しいとき)
        self.flat_cutout = False
        # 最後のフレームの人の距離 (m、中央値)
        self.person_depth = None
        # 受信を止めた後、アルファで切るように平らにしたか (release_to_matte) と、そのときのアルファの有無
        self._released = False
        self._idle_alpha = False
        # そのとき深度の無い人の画素を置く距離 (m)
        self.person_distance = 2.0
        self._previous = None
        self._previous_mask = None
        # 平面カットアウト用の人物マスク (ぼかし済み、grid.band の範囲)
        self._person_alpha = None
        self._socket = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        # (timecode か None, 処理済みの深度) を古い順に
        self._queue = collections.deque(maxlen=SYNC_QUEUE)
        self._grid = None
        # 実写の NDIInput (表示中の timecode を見て同じ時刻の深度を出す)
        self.video = None
        self.sync = True
        self.sync_offset = 0.0
        # 表示中の深度が映像よりどれだけ古いか (ms)。揃えていないときは None
        self.sync_lag = None
        # 最後にスクリーンへ出した形 (grid.key, 頂点, 面, マスク, meta)
        self.shown_state = None
        # 録画中 (take.Recorder) は処理した深度を全部ここへ渡す。
        # 受信スレッドから callable(timecode (100ns) か None, (grid.key, 頂点, 面, マスク, meta)) で呼ぶ
        self.frame_sink = None
        self._grids = {}

    @property
    def running(self):
        return self._running

    def start(self, source_name, library_override=""):
        """NDI の深度ソースから受ける。"""
        self.stop()
        self.receiver = ndi.Receiver(source_name, False, library_override, ndi.RECV_COLOR_BEST)
        self.source_name = source_name
        self._begin(self._recv_loop)

    def start_udp(self, port=UDP_DEFAULT_PORT):
        """trkAR の無圧縮 UDP 深度 ("ARD1") を受ける。"""
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 1 フレーム ≈ 100 パケットが一度に届くので受信バッファを大きめに
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        try:
            sock.bind(("0.0.0.0", port))
        except OSError:
            sock.close()
            raise
        sock.settimeout(0.1)
        self._socket = sock
        self.source_name = "UDP :%d" % port
        self._begin(self._udp_loop)

    def _begin(self, loop):
        self.frames_received = 0
        self.frames_shown = 0
        self.resolution = (0, 0)
        self.last_error = ""
        self._queue.clear()
        self.sync_lag = None
        self._previous = None
        self._previous_mask = None
        self._person_alpha = None
        self.has_people = False
        self.person_only = False
        self._released = False
        self._running = True
        self._thread = threading.Thread(target=loop, name="trkARDepthIn", daemon=True)
        self._thread.start()
        bpy.app.timers.register(self._tick, persistent=True)

    def stop(self):
        self._running = False
        if bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.unregister(self._tick)
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.receiver is not None:
            self.receiver.close()
            self.receiver = None
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        self._queue.clear()
        if self.video is not None and self.video.alpha_override == self._flat_cutout_alpha:
            self.video.alpha_override = None

    def idle_update(self, scene):
        """受信していない間の深度スクリーン (0.5 秒ごと)。止めた後に RVM を始めても、
        RVM が止まっても、固まった人物マスクが残らないようにする。"""
        if self._running or scene is None:
            return
        video = self.video
        alpha = bool(video is not None and video.running and video.has_alpha)
        if not self._released:
            if alpha and self.release_to_matte(scene):
                self._released = True
                self._idle_alpha = True
        elif alpha != self._idle_alpha:
            # アルファが無くなったら全面を透明に (平らな板が実写で画面を塞がないように)
            self._idle_alpha = alpha
            self._fill_mask(1.0 if alpha else 0.0)

    def _flat_cutout_alpha(self, width, height):
        """ndi_input.NDIInput.alpha_override 用。深度メッシュは使わず、最新の人物マスク
        (_person_alpha、ARKit の人物ビットだけをぼかしたもの。UDP 受信の解像度、上が上の並び) を
        実写映像の大きさに拡大して、Blender の画像と同じ左下原点の (height, width) float32 で返す。
        (_previous_mask は深度スクリーン用で、人物設定によっては背景の深度も含むので使わない)"""
        mask = self._person_alpha
        if mask is None or width <= 0 or height <= 0:
            return None
        mh, mw = mask.shape
        if mh == 0 or mw == 0:
            return None
        rows = np.arange(height) * mh // height
        cols = np.arange(width) * mw // width
        return mask[::-1][rows][:, cols].astype(np.float32)

    def _fill_mask(self, value):
        mask = bpy.data.images.get(MASK_IMAGE_NAME)
        if mask is not None:
            pixels = np.full(len(mask.pixels), value, dtype=np.float32)
            pixels[3::4] = 1.0
            mask.pixels.foreach_set(pixels)
            mask.update()

    def release_to_matte(self, scene):
        """受信を止めた後も実写のアルファ (RVM) で切り抜き続けられるようにする。
        そのままだと最後のフレームの人物マスクと形で固まり、動き続けるアルファの邪魔をする。
        マスクを全面「表示」にし、形は最後の人の距離に平らにする (頂点はカメラからの視線上を
        動かすだけなので、カメラから見た映像の位置は変わらない)。"""
        obj = bpy.data.objects.get(OBJECT_NAME)
        if obj is None or obj.type != "MESH" or obj.mode == "EDIT" or not len(obj.data.vertices):
            return False
        distance = (self.person_depth or scene.trkar.depth_person_distance) * scene.trkar.scale
        mesh = obj.data
        co = np.empty(len(mesh.vertices) * 3, dtype=np.float32)
        mesh.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        z = np.minimum(co[:, 2], -1e-6)
        co *= (-distance / z)[:, None]
        mesh.vertices.foreach_set("co", co.ravel())
        mesh.update()
        self._fill_mask(1.0)
        return True

    # --- 受信スレッド ---

    def _recv_loop(self):
        while self._running:
            receiver = self.receiver
            if receiver is None:
                return
            try:
                receiver.capture(self._on_frame, 100)
            except Exception as exc:
                self.last_error = str(exc)

    def _on_frame(self, frame):
        if frame.FourCC not in (ndi.FOURCC_PA16, ndi.FOURCC_P216):
            self.last_error = "深度ソースではありません (16bit 映像が来ていない)"
            return
        meta = parse_metadata(frame.p_metadata.decode("utf-8", "replace") if frame.p_metadata else "")
        if meta is None:
            self.last_error = "trkARDepth メタデータがありません"
            return
        width, height, stride = frame.xres, frame.yres, frame.line_stride_in_bytes
        plane = stride * height
        total = plane * (3 if frame.FourCC == ndi.FOURCC_PA16 else 2)
        raw = np.ctypeslib.as_array(ctypes.cast(frame.p_data, ctypes.POINTER(ctypes.c_uint8)), shape=(total,))
        depth = raw[:plane].view(np.uint16).reshape(height, stride // 2)[:, :width]
        if frame.FourCC == ndi.FOURCC_PA16:
            alpha = raw[plane * 2:plane * 3].view(np.uint16).reshape(height, stride // 2)[:, :width]
        else:
            alpha = None
        # NDI の圧縮で値が少し揺れるので 1/4・3/4 の中間で区切る
        confidence = None if alpha is None else (alpha > 16384).astype(np.uint8) + (alpha > 49152)
        self._process(depth.astype(np.float32) * meta.get("scale", 0.0001), confidence, meta,
                      timecode=frame.timecode)

    def _udp_loop(self):
        # frame_id → {"raw": UInt16 (h, w), "rows": 受信済みの行, "got": パケット数, "count": 総数, "meta": ...}
        frames = {}
        latest = -1
        last_raw = None
        while self._running:
            sock = self._socket
            if sock is None:
                return
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    self.last_error = str(exc)
                return
            if len(data) < UDP_HEADER.size or data[:4] not in UDP_MAGICS:
                continue
            (_, frame_id, width, height, row_start, row_count,
             _, packet_count, fx, fy, cx, cy, flags) = UDP_HEADER.unpack_from(data)
            offset = UDP_HEADER.size
            timecode = None
            if data[:4] == b"ARD3":
                if len(data) < offset + UDP_TIMECODE.size:
                    continue
                timecode = UDP_TIMECODE.unpack_from(data, offset)[0]
                offset += UDP_TIMECODE.size
            payload = np.frombuffer(data, dtype="<u2", offset=offset)
            if payload.size != row_count * width or row_start + row_count > height:
                continue
            entry = frames.get(frame_id)
            if entry is None:
                # 使い終わったフレームに遅れて来たパケットは無視する
                # (大きく戻ったときは trkAR 側で送り直しが始まったとみなして受ける)
                if latest >= 0 and (latest - frame_id) % (1 << 32) < 60:
                    continue
                # 次のフレームが来たのに揃っていないフレームは、ほぼ揃っていれば
                # 欠けた行を前のフレームで埋めて使う (Wi-Fi で 1 パケット落ちただけで捨てない)
                for old_id in list(frames):
                    old = frames.pop(old_id)
                    if old["got"] >= 0.8 * old["count"] and last_raw is not None \
                            and last_raw.shape == old["raw"].shape:
                        old["raw"][~old["rows"]] = last_raw[~old["rows"]]
                        latest = old_id
                        last_raw = self._deliver(old)
                entry = frames[frame_id] = {
                    "raw": np.zeros((height, width), np.uint16),
                    "rows": np.zeros(height, bool),
                    "got": 0, "count": packet_count,
                    "meta": {"fx": fx, "fy": fy, "cx": cx, "cy": cy},
                    "people": data[:4] != b"ARD1",
                    # bit 2: 深度なしで人物マスクだけ
                    "person_only": bool(flags & 4),
                    "timecode": timecode,
                }
            if entry["raw"].shape != (height, width):
                continue
            entry["raw"][row_start:row_start + row_count] = payload.reshape(row_count, width)
            entry["rows"][row_start:row_start + row_count] = True
            entry["got"] += 1
            if entry["got"] >= entry["count"]:
                del frames[frame_id]
                latest = frame_id
                last_raw = self._deliver(entry)

    def _deliver(self, entry):
        raw = entry["raw"]
        if entry["people"]:
            depth = (raw & 0x1FFF).astype(np.float32) * 0.001
            person = ((raw >> 13) & 1).astype(bool)
        else:
            depth = (raw & 0x3FFF).astype(np.float32) * 0.001
            person = None
        confidence = (raw >> 14).astype(np.uint8)
        try:
            self._process(depth, confidence, entry["meta"], person, entry["timecode"],
                          entry.get("person_only", False))
        except Exception as exc:
            self.last_error = str(exc)
        return raw

    def _process(self, depth_m, confidence, meta, person=None, timecode=None, person_only=False):
        """深度 (メートル、(h, w))・信頼度 (0..2 か None)・人物マスク (bool か None) から
        頂点とマスクを作る。person_only は深度の取れない機種からの人物マスクだけのフレーム
        (人だけ表示し、深度の無い人の画素は person_distance に置く)。"""
        height, width = depth_m.shape
        grid = self._grid
        if grid is None or grid.key != (width, height, self.step):
            grid = self._grid = Grid(width, height, self.step)
            self._previous = None
            self._previous_mask = None
        self.has_people = person is not None
        person_only = person_only and person is not None
        self.person_only = person_only

        # カラーと重なる範囲だけを全解像度で処理し、最後に格子の画素を拾う
        band = grid.band
        if person is not None:
            # 平面カットアウト用 (人物設定・深度の有無に関係なく人物ビットだけで決める)
            person_alpha = _blur(person[band].astype(np.float32), MASK_BLUR_PASSES)
            previous_alpha = self._person_alpha
            if self.temporal > 0.0 and previous_alpha is not None and previous_alpha.shape == person_alpha.shape:
                weight = 0.5 * self.temporal
                person_alpha = previous_alpha * weight + person_alpha * (1.0 - weight)
            self._person_alpha = person_alpha
        else:
            self._person_alpha = None
        d = depth_m[band]
        max_distance = self.max_distance
        valid = (d > 0.0) & (d <= max_distance)
        if confidence is not None:
            valid &= confidence[band] >= self.min_confidence

        people = "ONLY" if person_only else (self.people if person is not None else "OFF")
        matte = self.video_alpha and person is not None
        if matte:
            # 輪郭は映像のアルファで切る。人物マスクは人の深度を埋めるのにだけ使う
            people = "MATTE"
        if people != "OFF":
            p = person[band]
            self.person_ratio = float(p.mean())
            # 人の輪郭では深度に背景がにじんだり抜けたりするので、近く (3 画素) の
            # 人の画素のうち一番手前の深度で置き換える (背景は必ず人より奥)
            near = np.where(valid & p, d, np.inf).astype(np.float32)
            for _ in range(3):
                near = _spread_min(near)
            fix = p & np.isfinite(near) & (~valid | (d > near + PERSON_BLEED))
            d = np.where(fix, near, d)
            valid = valid | fix
            # 人の中の大きめの穴も、埋まるまで周りの深度を広げる (残すと 1 点だけ奥に飛ぶ)
            hole = p & ~valid
            for _ in range(24):
                if not hole.any():
                    break
                near = _spread_min(near)
                fill = hole & np.isfinite(near)
                d = np.where(fill, near, d)
                valid = valid | fill
                hole &= ~fill
            if matte:
                # RVM の輪郭 (髪など) は ARKit のマスクより外へ出るので、少し外まで人の深度にする
                grow = _dilate(p, MATTE_GROW) & ~p
                for _ in range(MATTE_GROW):
                    near = _spread_min(near)
                ring = grow & np.isfinite(near) & (~valid | (d > near + PERSON_BLEED))
                d = np.where(ring, near, d)
                valid = valid | ring
                p = p | grow
                hole |= grow & ~valid
            if person_only:
                # 推定深度も無い所 (前面カメラなど) は決めた距離の板にする
                d = np.where(hole, np.float32(self.person_distance), d)
                valid = valid | hole
            seen = valid & p
            if seen.any():
                # 受信を止めたときに平らに置く距離 (release_to_matte)
                self.person_depth = float(np.median(d[seen]))
        else:
            p = None
            self.person_ratio = 0.0

        if self.denoise:
            # 3×3 のメディアン。近傍が全部有効な画素だけ (境目の値を混ぜない)
            stack = np.stack([_shifted(d, dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
            vstack = np.stack([_shifted(valid, dy, dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1)])
            d = np.where(valid & vstack.all(axis=0), np.median(stack, axis=0), d)

        previous = self._previous
        if self.temporal > 0.0 and previous is not None and previous[0].shape == d.shape:
            prev_d, prev_valid = previous
            # 大きく動いた画素 (人が動いたなど) は混ぜずにすぐ追従させる
            steady = valid & prev_valid & (np.abs(d - prev_d) < 0.05 * d)
            d = np.where(steady, prev_d * self.temporal + d * (1.0 - self.temporal), d)
        self._previous = (d, valid)

        # 前後の段差: 4 近傍のどれかと距離がこの割合以上違う画素は境目として抜く
        edge = np.zeros_like(valid)
        limit = 1.0 + self.edge_tolerance
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0)):
            nd = _shifted(d, dy, dx)
            nv = _shifted(valid, dy, dx)
            edge |= nv & ((nd > d * limit) | (d > nd * limit))
        # 境目は 1 画素広げてから抜く (ぼかした後も手前と奥の間がちゃんと 0 近くまで落ちるように)
        edge = _dilate(edge, 1)
        shown = valid
        if people == "MATTE":
            # 見える範囲は映像のアルファが決めるので、人 (と少し外側) は段差でも削らない
            edge &= ~_dilate(p, 1)
            if person_only:
                shown = valid & p
        elif p is not None:
            # 人の輪郭は深度の段差ではなく人物マスクで決める (カメラ映像の輪郭に沿う)。
            # 人の側は削らず、背景の側だけ抜く
            edge &= ~(p & _dilate(~p, 2))
            edge |= _dilate(~p & _dilate(p, 1), 2) & ~p
            if people == "ONLY":
                shown = valid & p
        mask = _blur((shown & ~edge).astype(np.float32), MASK_BLUR_PASSES)
        previous_mask = self._previous_mask
        if self.temporal > 0.0 and previous_mask is not None and previous_mask.shape == mask.shape:
            # 人物マスクは毎フレーム少し揺れるので、輪郭も軽く時間方向にならす
            weight = 0.5 * self.temporal
            mask = previous_mask * weight + mask * (1.0 - weight)
        self._previous_mask = mask
        # Blender の画像は左下原点
        mask_pixels = np.repeat(mask[::-1, :, None], 4, axis=2)
        mask_pixels[..., 3] = 1.0

        rows = grid.rows - grid.top
        cols = grid.cols - grid.left
        gd = d[np.ix_(rows, cols)]
        gvalid = valid[np.ix_(rows, cols)]
        # 無効な画素は最大距離に置いておく (マスクで透明になる)
        gd = np.where(gvalid, gd, max_distance)

        x = (grid.cols[None, :] - meta["cx"]) / meta["fx"] * gd
        y = -(grid.rows[:, None] - meta["cy"]) / meta["fy"] * gd
        co = np.stack([x, y, -gd], axis=-1).astype(np.float32).reshape(-1)

        corners = np.stack([gd[:-1, :-1], gd[1:, :-1], gd[1:, 1:], gd[:-1, 1:]])
        gshown = shown[np.ix_(rows, cols)]
        corner_valid = gshown[:-1, :-1] & gshown[1:, :-1] & gshown[1:, 1:] & gshown[:-1, 1:]
        flat = corners.max(axis=0) <= corners.min(axis=0) * limit
        face_valid = (corner_valid & flat).astype(np.float32).reshape(-1)

        with self._lock:
            self._queue.append((timecode, (grid, co, face_valid, mask_pixels, meta)))
        self.frames_received += 1
        sink = self.frame_sink
        if sink is not None:
            sink(timecode, (grid.key, co, face_valid, mask_pixels, meta))

    # --- メインスレッド ---

    def _tick(self):
        if not self._running:
            return None
        scene = bpy.context.scene
        if scene is not None:
            settings = scene.trkar
            self.step = int(settings.depth_step)
            self.max_distance = settings.depth_max_distance
            self.min_confidence = int(settings.depth_confidence)
            self.edge_tolerance = settings.depth_edge_tolerance
            self.denoise = settings.depth_denoise
            self.temporal = settings.depth_temporal
            self.people = settings.depth_people
            self.person_distance = settings.depth_person_distance
            self.sync = settings.depth_sync
            self.sync_offset = settings.depth_sync_offset / 1000.0
            self.flat_cutout = settings.depth_flat_cutout
        video = self.video
        if video is not None:
            if self.flat_cutout:
                video.alpha_override = self._flat_cutout_alpha
            elif video.alpha_override == self._flat_cutout_alpha:
                video.alpha_override = None
        self.video_alpha = bool(video is not None and video.running and video.has_alpha)
        pending = self._take()
        if pending is not None and scene is not None:
            try:
                self._apply(scene, *pending)
            except Exception as exc:
                self.last_error = str(exc)
        return 1.0 / 60.0

    def _take(self):
        """次に出す深度。実写を受けていれば、表示中の映像と同じ時刻 (以前) の最新の深度を選ぶ。
        UDP の深度は NDI の映像より先に届くので、そのまま出すと輪郭が映像より先に動く。"""
        video = self.video
        target = None
        if self.sync and video is not None and video.running and video.shown_timecode is not None:
            target = video.shown_timecode - int(self.sync_offset * 10_000_000)
        with self._lock:
            queue = self._queue
            if not queue:
                return None
            newest = queue[-1][0]
            if target is None or newest is None or abs(newest - target) > SYNC_LIMIT:
                # 時刻が無い / 時計が合わないときは最新をすぐ出す
                self.sync_lag = None
                item = queue[-1]
                queue.clear()
                return item[1]
            chosen = None
            while queue and queue[0][0] is not None and queue[0][0] <= target:
                chosen = queue.popleft()
        if chosen is None:
            # 深度の方が新しい: 映像が追いつくまで今の形のまま待つ
            return None
        self.sync_lag = (target - chosen[0]) / 10_000.0
        return chosen[1]

    def apply_recorded(self, scene, key, co, face_valid, mask_pixels, meta):
        """録画した形をそのまま深度スクリーンに出す (テイク再生)。"""
        key = tuple(int(v) for v in key)
        grid = self._grids.get(key)
        if grid is None:
            grid = self._grids[key] = Grid(*key)
        self._apply(scene, grid, co, face_valid, mask_pixels, meta)

    def _apply(self, scene, grid, co, face_valid, mask_pixels, meta):
        obj = bpy.data.objects.get(OBJECT_NAME)
        if obj is None or obj.type != "MESH" or obj.mode == "EDIT":
            return
        mesh = obj.data
        if list(mesh.get("trkar_grid", mesh.get("artrk_grid", []))) != list(grid.key) or len(mesh.vertices) * 3 != len(co):
            build_mesh(mesh, grid)
        if not self.frames_shown:
            update_edge(scene.trkar)
        height, width = mask_pixels.shape[:2]
        mask = ensure_mask_image(width, height)
        if tuple(mask.size) != (width, height):
            mask.scale(width, height)
        mask.pixels.foreach_set(mask_pixels.ravel())
        mask.update()
        # 実寸 (m) をトラッキングのスケールに合わせる (カメラの移動量と同じ倍率)
        mesh.vertices.foreach_set("co", co * scene.trkar.scale)
        mesh.attributes[VALID_ATTRIBUTE].data.foreach_set("value", face_valid)
        mesh.update()
        self.resolution = (grid.width, grid.height)
        self.valid_ratio = float(face_valid.mean()) if len(face_valid) else 0.0
        self.frames_shown += 1
        self.shown_state = (grid.key, co, face_valid, mask_pixels, meta)

        settings = scene.trkar
        background = bpy.data.objects.get(BACKGROUND_NAME)
        if settings.depth_background:
            background = ensure_background(obj)
            place_background(background, grid, meta, settings.depth_background_distance * settings.scale)
        if background is not None:
            hidden = not settings.depth_background
            if background.hide_viewport != hidden:
                background.hide_viewport = hidden
                background.hide_render = hidden
