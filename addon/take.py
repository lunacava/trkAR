"""録画 (REC) とテイクの再生。

REC 中はタイムラインを再生し、カメラの動きをテイク用のアクションにキーフレームで入れる
(tracking.tick の「再生中にキーフレーム記録」)。同時にフレームが進むたびに、表示中の実写
(アルファ込み) と深度スクリーンの形をテイクのフォルダへ書く:

    <テイク>/take.json          fps・フレーム範囲・カメラ・アクション名と、撮影の来歴
                                (スレート・場所・撮影環境・撮った機種・製作者。shot_info)
    <テイク>/video/000001.jpg   実写の色
    <テイク>/alpha/000001.png   実写のアルファ (RVM の切り抜きを受けていたときだけ。白黒)
    <テイク>/depth/000001.npz   深度スクリーンの頂点・面・マスク
    <テイク>/render.mp4         書き出し (PNG 連番なら render/0001.png …)

テイク再生中は受信を止め、フレームが変わるたびに同じフレームの実写と深度を読み込む。
スクリーン類は "ARtrk NDI" などライブと同じ画像を見ているので、そのまま再生・レンダーできる。
"""

import json
import math
import os
import platform
import shutil
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import bpy
import numpy as np
from mathutils import Euler, Matrix, Quaternion, Vector

# 再生中だけカメラをこの Empty の子にする。動かす・回すとテイクのカメラの軌跡ごと背景の中でずれる
ANCHOR_NAME = "ARtrk テイクの置き場"

# 書き込み待ちがこれ以上たまったらそのフレームは捨てる (後で前のフレームで埋める)
MAX_IN_FLIGHT = 12
# 録画中はタイムラインの終わりをここまで延ばす (分)
MAX_MINUTES = 30


def takes_folder(configured):
    """テイクを置くフォルダ。空なら .blend の隣の artrk_takes (保存していなければ ~/Movies/ARtrk Takes、Windows は ~/Videos)。"""
    if not configured:
        configured = "//artrk_takes"
    if configured.startswith("//") and not bpy.data.filepath:
        default = "~/Videos/ARtrk Takes" if sys.platform == "win32" else "~/Movies/ARtrk Takes"
        return os.path.expanduser(default)
    return os.path.normpath(bpy.path.abspath(configured))


def write_image(path, data):
    """(h, w, c) uint8 (左上原点) を書く。拡張子で JPEG / PNG。"""
    import OpenImageIO as oiio

    if data.ndim == 2:
        data = data[..., None]
    height, width = data.shape[:2]
    data = np.ascontiguousarray(data)
    spec = oiio.ImageSpec(width, height, data.shape[2], "uint8")
    if path.endswith(".png"):
        spec.attribute("png:compressionLevel", 1)
        # 渡すのはストレートアルファ (RVM のまま)。乗算済みとみなして割り戻さない
        spec.attribute("oiio:UnassociatedAlpha", 1)
    else:
        spec.attribute("Compression", "jpeg:92")
    out = oiio.ImageOutput.create(path)
    if out is None or not out.open(path, spec):
        raise OSError("書けません: %s" % path)
    try:
        out.write_image(data)
    finally:
        out.close()


def read_image(path):
    """write_image の逆。(h, w, 4) uint8 (左上原点)。白黒は RGB に広げる。"""
    import OpenImageIO as oiio

    config = oiio.ImageSpec()
    # PNG のアルファを読むときに色へ掛けない (書いたときのストレートアルファのまま)
    config.attribute("oiio:UnassociatedAlpha", 1)
    inp = oiio.ImageInput.open(path, config)
    if inp is None:
        raise OSError("読めません: %s" % path)
    try:
        pixels = inp.read_image(0, 0, 0, inp.spec().nchannels, "uint8")
    finally:
        inp.close()
    if pixels.ndim == 2:
        pixels = pixels[..., None]
    if pixels.shape[2] == 4:
        return pixels
    rgba = np.full(pixels.shape[:2] + (4,), 255, np.uint8)
    rgba[..., :3] = pixels[..., :3] if pixels.shape[2] >= 3 else pixels[..., :1]
    return rgba


def _frame_name(frame):
    return "%06d" % frame


def load_info(path):
    with open(os.path.join(path, "take.json"), encoding="utf-8") as f:
        return json.load(f)


def slate_label(scene_no, cut_no, take_no):
    return "S%02dC%02dT%02d" % (scene_no, cut_no, take_no)


def _or_none(text):
    """空の文字は None (take.json では分からない値を空文字でなく null にする決まり)"""
    text = text.strip() if isinstance(text, str) else text
    return text or None


def _os_label():
    if sys.platform == "darwin":
        version = platform.mac_ver()[0]
        return "macOS %s" % version if version else "macOS"
    if sys.platform == "win32":
        # release() は Windows 11 でも "10" になることがあるので、ビルド番号まで入った version() を使う
        return "Windows %s" % platform.version()
    return _or_none("%s %s" % (platform.system(), platform.release()))


def _device_info(pose, client):
    """録画を始めたときに追従していた iPhone。pose は /client/current、client は /client/info (どちらも None あり)。
    /client/info が届いていなければ機種・OS・アプリの版・端末名は None。"""
    app = None
    if client is not None and client.app_version:
        app = "%s (%s)" % (client.app_version, client.app_build) if client.app_build else client.app_version
    system_os = None
    if client is not None and client.system_version:
        system_os = _or_none("%s %s" % (client.system_name, client.system_version))
    return {
        "system": "ARtrk" if pose is not None or client is not None else None,
        "model": client.model if client is not None else None,
        "identifier": _or_none(client.identifier) if client is not None else None,
        "os": system_os,
        "app": app,
        "device_name": _or_none(client.device_name) if client is not None else None,
        "client_id": pose.client_id if pose is not None else (client.client_id if client is not None else None),
        # iPhone のタイムコードの設定 (Gen なら None)
        "fps": pose.timecode_fps if pose is not None else None,
        "hfov": round(pose.hfov_deg, 2) if pose is not None and pose.hfov_deg > 0.0 else None,
        "tc_source": pose.tc_source_name if pose is not None else None,
    }


def shot_info(scene, slate, site, host, addon, people, pose=None, client=None):
    """録画を始めたときの来歴。take.json にそのまま足す (ライブラリ側が読んで来歴に取り込む)。
    撮った機種は device (camera は前から Blender のカメラのオブジェクト名)。
    slate = (production, scene, cut, take)、people = {"creator": …, "operator": …}。分からない値は None。"""
    production, scene_no, cut_no, take_no = slate
    blend = os.path.splitext(os.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else None
    return {
        # タイムゾーン付きのローカル時刻 (例 2026-10-13T21:30:00+09:00)
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "slate": {"production": _or_none(production), "scene": scene_no, "cut": cut_no, "take": take_no,
                  "label": slate_label(scene_no, cut_no, take_no)},
        "location": {"blend": blend, "bpy_scene": scene.name},
        "environment": {
            "site": _or_none(site),
            "host": _or_none(host),
            "hostname": _or_none(socket.gethostname()),
            "os": _os_label(),
            "blender": "%d.%d.%d" % tuple(bpy.app.version),
            "addon": addon,
        },
        "device": _device_info(pose, client),
        "people": {key: _or_none(value) for key, value in people.items()},
    }


def _camera_of(scene):
    camera = scene.artrk.camera or scene.camera
    return camera if camera is not None and camera.type == "CAMERA" else None


def _assign(id_data, action):
    """アクションを割り当てる (Blender 4.4 以降はスロットも)。前のアクションを返す。"""
    ad = id_data.animation_data or id_data.animation_data_create()
    previous = ad.action
    ad.action = action
    if action is not None and hasattr(ad, "action_slot") and ad.action_slot is None:
        slots = [s for s in getattr(action, "slots", []) if s.target_id_type == id_data.id_type]
        if slots:
            ad.action_slot = slots[0]
    return previous


def _action_fcurves(action):
    """アクションの F カーブを全部。Blender 5.0 で action.fcurves が無くなり、
    レイヤー → ストリップ → スロットごとの channelbag の下に移った (4.4 から)。"""
    if not hasattr(action, "layers"):
        return list(action.fcurves)
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            for slot in action.slots:
                bag = strip.channelbag(slot)
                if bag is not None:
                    curves.extend(bag.fcurves)
    return curves


def _new_fcurves(action, id_type):
    """F カーブを足す先。Blender 4.4 以降は id_type のスロットの channelbag、それより前は action.fcurves。"""
    if not hasattr(action, "layers"):
        return action.fcurves
    slot = next((sl for sl in action.slots if sl.target_id_type == id_type), None) \
        or action.slots.new(id_type=id_type, name=action.name)
    layer = action.layers[0] if action.layers else action.layers.new("Layer")
    strip = layer.strips[0] if layer.strips else layer.strips.new(type="KEYFRAME")
    return strip.channelbag(slot, ensure=True).fcurves


def _action_to_dict(action):
    """アクションの F カーブをまるごと辞書に (テイクフォルダへ書き出して .blend の外でも読めるように)。"""
    curves = []
    for fc in _action_fcurves(action):
        points = [
            {
                "co": tuple(kp.co),
                "interpolation": kp.interpolation,
                "easing": kp.easing,
                "handle_left": tuple(kp.handle_left),
                "handle_right": tuple(kp.handle_right),
                "handle_left_type": kp.handle_left_type,
                "handle_right_type": kp.handle_right_type,
            }
            for kp in fc.keyframe_points
        ]
        curves.append({"data_path": fc.data_path, "array_index": fc.array_index, "points": points})
    return {"name": action.name, "curves": curves}


def _action_from_dict(data, id_type):
    """_action_to_dict の逆。同名のアクションが既にあれば作り直さずそれを使う。
    id_type はアクションを割り当てる相手 (カメラのオブジェクトは OBJECT、レンズは CAMERA)。"""
    name = data["name"]
    action = bpy.data.actions.get(name) or bpy.data.actions.new(name)
    action.use_fake_user = True
    fcurves = _new_fcurves(action, id_type)
    for curve in data["curves"]:
        fc = fcurves.find(curve["data_path"], index=curve["array_index"]) \
            or fcurves.new(curve["data_path"], index=curve["array_index"])
        fc.keyframe_points.clear()
        for point in curve["points"]:
            kp = fc.keyframe_points.insert(point["co"][0], point["co"][1])
            kp.interpolation = point["interpolation"]
            kp.easing = point["easing"]
            kp.handle_left = point["handle_left"]
            kp.handle_right = point["handle_right"]
            kp.handle_left_type = point["handle_left_type"]
            kp.handle_right_type = point["handle_right_type"]
        fc.update()
    return action


def _save_actions(path, action, lens_action):
    """録画終了時に、テイク単体でも読み込めるようアクションをディスクへも残す。"""
    data = {"action": _action_to_dict(action)}
    if lens_action is not None:
        data["lens_action"] = _action_to_dict(lens_action)
    with open(os.path.join(path, "actions.json"), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load_or_restore_action(path, name, key):
    """.blend にあれば名前で拾い、なければ actions.json (テイクフォルダ) から復元する。"""
    action = bpy.data.actions.get(name)
    if action is not None or not name:
        return action
    actions_path = os.path.join(path, "actions.json")
    if not os.path.exists(actions_path):
        return None
    with open(actions_path, encoding="utf-8") as f:
        data = json.load(f)
    entry = data.get(key)
    if entry is None:
        return None
    return _action_from_dict(entry, "CAMERA" if key == "lens_action" else "OBJECT")


def _action_matrix(action, frame):
    """アクションのそのフレームでの位置・回転 (オブジェクトの matrix_basis 相当、拡大は無視)"""
    values = {}
    for fc in _action_fcurves(action):
        values[(fc.data_path, fc.array_index)] = fc.evaluate(frame)
    location = Vector([values.get(("location", i), 0.0) for i in range(3)])
    if any(path == "rotation_quaternion" for path, _i in values):
        rotation = Quaternion([values.get(("rotation_quaternion", i), 1.0 if i == 0 else 0.0) for i in range(4)])
        rotation.normalize()
    else:
        rotation = Euler([values.get(("rotation_euler", i), 0.0) for i in range(3)]).to_quaternion()
    return Matrix.Translation(location) @ rotation.to_matrix().to_4x4()


def anchor_pivot(action, frame, distance):
    """置き場の基準にするテイクの位置と向き (Matrix)。位置は始めのフレームでカメラの `distance` 前
    (人の居る所) を床 (Z=0) に下ろした所、+Y はそのときカメラが見ている向き (水平)。
    置き場をこれに重ねれば録ったときのまま、動かせばその分だけ、回せばここを中心に回る。
    テイクが違っても置き場の Y の矢印がカメラの見る向きになる。"""
    basis = _action_matrix(action, frame)
    forward = basis.to_3x3() @ Vector((0.0, 0.0, -1.0))
    forward.z = 0.0
    if forward.length < 1e-4:
        forward = Vector((0.0, 1.0, 0.0))
    forward.normalize()
    point = basis.translation + forward * distance
    point.z = 0.0
    heading = math.atan2(forward.y, forward.x) - math.pi / 2.0
    return Matrix.Translation(point) @ Matrix.Rotation(heading, 4, "Z")


class Recorder:
    def __init__(self, ndi_in, depth_in):
        self.ndi_in = ndi_in
        self.depth_in = depth_in
        self.path = ""
        self.start_frame = 1
        self.last_frame = 0
        self.frames = 0
        self.dropped = 0
        self.errors = 0
        self.last_error = ""
        self.started_at = 0.0
        self._recording = False
        self._pool = None
        self._in_flight = 0
        self._lock = threading.Lock()
        self._written = set()
        self._alpha = None
        self._restore = {}
        self._stopped_playing_at = None
        self._action = None
        self._lens_action = None
        self._info = {}

    @property
    def recording(self):
        return self._recording

    @property
    def seconds(self):
        return time.monotonic() - self.started_at if self._recording else 0.0

    @property
    def slate_label(self):
        """録画中のテイクのスレート (例 S01C03T02)。来歴なしで始めたときは空"""
        return (self._info.get("slate") or {}).get("label") or "" if self._recording else ""

    def start(self, context, folder, shot=None):
        """shot は take.json に足す撮影の来歴 (shot_info)。"""
        scene = context.scene
        settings = scene.artrk
        camera = _camera_of(scene)
        if camera is None:
            raise RuntimeError("録画するカメラがありません")
        name = time.strftime("take_%Y%m%d_%H%M%S")
        path = os.path.join(folder, name)
        os.makedirs(os.path.join(path, "video"), exist_ok=True)
        os.makedirs(os.path.join(path, "depth"), exist_ok=True)
        os.makedirs(os.path.join(path, "alpha"), exist_ok=True)

        # テイクごとに新しいアクションへ記録する (前のテイクや手で打ったキーは残る)
        action = bpy.data.actions.new("ARtrk " + name)
        action.use_fake_user = True
        lens_action = bpy.data.actions.new("ARtrk " + name + " Lens")
        lens_action.use_fake_user = True
        self._restore = {
            "camera": camera.name,
            "action": _assign(camera, action),
            "lens_action": _assign(camera.data, lens_action),
            "record": settings.record,
            "frame_end": scene.frame_end,
            "sync_mode": scene.sync_mode,
        }
        self._action = action
        self._lens_action = lens_action
        self.path = path
        self.start_frame = scene.frame_start
        self.last_frame = self.start_frame - 1
        self.frames = self.dropped = self.errors = 0
        self.last_error = ""
        self._written = set()
        self._alpha = None
        self._in_flight = 0
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ARtrkTake")
        self._info = {
            "name": name,
            "fps": scene.render.fps / scene.render.fps_base,
            "frame_start": self.start_frame,
            "camera": camera.name,
            "action": action.name,
            "lens_action": lens_action.name,
        }
        self._info.update(shot or {})

        settings.record = True
        scene.frame_end = self.start_frame + int(self._info["fps"] * 60 * MAX_MINUTES)
        # 実時間に合わせる (重いときはフレームを飛ばす。飛んだフレームは後で埋める)
        scene.sync_mode = "FRAME_DROP"
        scene.frame_set(self.start_frame)
        self.ndi_in.keep_rgba = True
        self._recording = True
        self.started_at = time.monotonic()
        self._stopped_playing_at = None
        bpy.app.handlers.frame_change_post.append(self._on_frame)
        if not bpy.app.timers.is_registered(self._watch):
            bpy.app.timers.register(self._watch, first_interval=0.5)
        self._capture(scene)
        if not context.screen.is_animation_playing:
            bpy.ops.screen.animation_play()
        return path

    def _watch(self):
        """再生が (スペースキーなどで) 止められたら録画も終える。"""
        if not self._recording:
            return None
        screen = bpy.context.screen
        playing = any(w.screen.is_animation_playing for w in bpy.context.window_manager.windows) \
            if screen is None else screen.is_animation_playing
        if playing:
            self._stopped_playing_at = None
        elif self._stopped_playing_at is None:
            self._stopped_playing_at = time.monotonic()
        elif time.monotonic() - self._stopped_playing_at > 0.5:
            self.stop(bpy.context)
            return None
        return 0.2

    def _on_frame(self, scene, _depsgraph=None):
        if self._recording:
            self._capture(scene)

    def _capture(self, scene):
        frame = scene.frame_current
        if frame < self.start_frame or frame in self._written:
            return
        with self._lock:
            if self._in_flight >= MAX_IN_FLIGHT:
                self.dropped += 1
                return
            self._in_flight += 1
        self._written.add(frame)
        self.last_frame = max(self.last_frame, frame)
        rgba = self.ndi_in.shown_rgba if self.ndi_in.running else None
        if rgba is not None and self._alpha is None:
            # 最初のフレームで決める (途中で RVM を付け外ししても拡張子は変えない)
            self._alpha = bool(self.ndi_in.has_alpha)
        state = self.depth_in.shown_state if self.depth_in.running else None
        self._pool.submit(self._write, frame, rgba, self._alpha, state)
        self.frames += 1

    def _write(self, frame, rgba, alpha, state):
        try:
            name = _frame_name(frame)
            if rgba is not None:
                # アルファ付きの PNG は 1080p で 1 枚 4〜5MB になるので、色は JPEG、アルファは白黒の PNG に分ける
                write_image(os.path.join(self.path, "video", name + ".jpg"), rgba[..., :3])
                if alpha:
                    write_image(os.path.join(self.path, "alpha", name + ".png"), rgba[..., 3])
            if state is not None:
                key, co, face_valid, mask_pixels, meta = state
                # マスクは RGB が同じ値 (A = 1) なので 1 チャンネル 8bit で持つ
                mask = (mask_pixels[..., 0] * 255.0 + 0.5).astype(np.uint8)
                np.savez_compressed(os.path.join(self.path, "depth", name + ".npz"), key=np.array(key), co=co,
                                    face_valid=face_valid.astype(np.uint8), mask=mask, meta=json.dumps(meta))
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)
        finally:
            with self._lock:
                self._in_flight -= 1

    def stop(self, context):
        if not self._recording:
            return None
        self._recording = False
        if self._on_frame in bpy.app.handlers.frame_change_post:
            bpy.app.handlers.frame_change_post.remove(self._on_frame)
        if context.screen is not None and context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        self.ndi_in.keep_rgba = False
        self._pool.shutdown(wait=True)
        self._pool = None

        scene = context.scene
        restore = self._restore
        settings = scene.artrk
        settings.record = restore["record"]
        scene.sync_mode = restore["sync_mode"]
        end = max(self.last_frame, self.start_frame)
        scene.frame_end = end
        self._fill_gaps(end)
        # ライブに戻る: カメラには録画前のアクションを戻す (テイクは「再生」で割り当てる)
        camera = bpy.data.objects.get(restore["camera"])
        if camera is not None:
            _assign(camera, restore["action"])
            _assign(camera.data, restore["lens_action"])
        info = dict(self._info, frame_end=end, alpha=bool(self._alpha),
                    video=self._alpha is not None, frames=self.frames, dropped=self.dropped)
        with open(os.path.join(self.path, "take.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        # カメラの動き自体もテイクフォルダへ焼く。.blend を保存し忘れたり、別の .blend で
        # 開いたりしてもこのテイクだけで再生できるように (アクションは .blend の中にしかない)。
        _save_actions(self.path, self._action, self._lens_action)
        return self.path

    def _fill_gaps(self, end):
        """飛んだフレームを前のフレームで埋める (再生・書き出しで欠けないように)。"""
        for sub in ("video", "alpha", "depth"):
            folder = os.path.join(self.path, sub)
            files = {os.path.splitext(n)[0]: n for n in os.listdir(folder)}
            if not files:
                continue
            ext = os.path.splitext(next(iter(files.values())))[1]
            # 頭の欠け (REC 直後はまだ表示中のフレームを持っていない) は最初のフレームで埋める
            previous = files[min(files)]
            for frame in range(self.start_frame, end + 1):
                name = _frame_name(frame)
                if name in files:
                    previous = files[name]
                elif previous is not None:
                    target = os.path.join(folder, name + ext)
                    try:
                        os.link(os.path.join(folder, previous), target)
                    except OSError:
                        shutil.copyfile(os.path.join(folder, previous), target)


class Player:
    """テイクを再生する。フレームが変わるたびにそのフレームの実写と深度を読み込む。"""

    def __init__(self, ndi_in, depth_in):
        self.ndi_in = ndi_in
        self.depth_in = depth_in
        self.path = ""
        self.info = None
        self.last_error = ""
        self._loaded = None
        self._restore = None
        # 再生中のテイクの置き場の基準 (anchor_pivot)
        self.pivot = Matrix()

    @property
    def active(self):
        return self.info is not None

    def start(self, scene, path):
        self.stop(scene)
        info = load_info(path)
        camera = bpy.data.objects.get(info.get("camera", "")) or _camera_of(scene)
        if camera is None:
            raise RuntimeError("テイクのカメラがありません")
        # .blend にアクションが残っていればそれを使い、なければテイクフォルダの
        # actions.json から復元する (別の .blend で開いた、保存し忘れた等でも再生できる)。
        action = _load_or_restore_action(path, info.get("action", ""), "action")
        lens_action = _load_or_restore_action(path, info.get("lens_action", ""), "lens_action")
        if action is None:
            raise RuntimeError(
                "テイクのアクション %s がこの .blend にも actions.json にもありません"
                % info.get("action")
            )
        self._restore = {
            "camera": camera.name,
            "action": _assign(camera, action),
            "lens_action": _assign(camera.data, lens_action) if lens_action is not None else None,
            "has_lens": lens_action is not None,
            "lock": scene.render.use_lock_interface,
            "parent": camera.parent.name if camera.parent is not None else "",
            "parent_inverse": camera.matrix_parent_inverse.copy(),
            "rotation_mode": camera.rotation_mode,
        }
        # 回転の F カーブと同じ回転モードにする (別の .blend のオイラーのカメラだと、
        # クォータニオンで録った向きが効かず位置しか動かない)
        paths = {fc.data_path for fc in _action_fcurves(action)}
        if "rotation_quaternion" in paths:
            camera.rotation_mode = "QUATERNION"
        elif "rotation_euler" in paths and camera.rotation_mode in ("QUATERNION", "AXIS_ANGLE"):
            camera.rotation_mode = "XYZ"
        # 置き場があれば再生中だけその子にする (アクションは録ったままのワールド座標なので、
        # 置き場が基準の点にあるとき元の位置と同じになるよう親の逆行列で戻しておく)
        self.pivot = anchor_pivot(action, info["frame_start"], scene.artrk.ndi_in_front_distance)
        anchor = bpy.data.objects.get(ANCHOR_NAME)
        if anchor is not None and anchor is not camera:
            camera.parent = anchor
            camera.matrix_parent_inverse = self.pivot.inverted()
        self.path = path
        self.info = info
        self.last_error = ""
        self._loaded = None
        scene.frame_start = info["frame_start"]
        scene.frame_end = info["frame_end"]
        # 描画中にハンドラーから画像を書き換えるので、レンダー中の UI 操作を止める
        scene.render.use_lock_interface = True
        bpy.app.handlers.frame_change_pre.append(self._on_frame)
        if not scene.frame_start <= scene.frame_current <= scene.frame_end:
            scene.frame_set(scene.frame_start)
        self.load(scene, scene.frame_current)

    def stop(self, scene):
        if self._on_frame in bpy.app.handlers.frame_change_pre:
            bpy.app.handlers.frame_change_pre.remove(self._on_frame)
        restore, self._restore = self._restore, None
        self.info = None
        if restore is None:
            return
        camera = bpy.data.objects.get(restore["camera"])
        if camera is not None:
            _assign(camera, restore["action"])
            if restore["has_lens"]:
                _assign(camera.data, restore["lens_action"])
            camera.parent = bpy.data.objects.get(restore["parent"]) if restore["parent"] else None
            camera.matrix_parent_inverse = restore["parent_inverse"]
            camera.rotation_mode = restore["rotation_mode"]
        if scene is not None:
            scene.render.use_lock_interface = restore["lock"]

    def _on_frame(self, scene, _depsgraph=None):
        if self.info is not None:
            self.load(scene, scene.frame_current)

    def load(self, scene, frame):
        info = self.info
        frame = min(max(frame, info["frame_start"]), info["frame_end"])
        if frame == self._loaded:
            return
        self._loaded = frame
        name = _frame_name(frame)
        try:
            if info.get("video", True):
                video = os.path.join(self.path, "video", name + ".jpg")
                if os.path.exists(video):
                    rgba = read_image(video)
                    alpha = os.path.join(self.path, "alpha", name + ".png")
                    if info.get("alpha") and os.path.exists(alpha):
                        rgba[..., 3] = read_image(alpha)[..., 0]
                    self.ndi_in.show_rgba(rgba)
            depth = os.path.join(self.path, "depth", name + ".npz")
            if os.path.exists(depth):
                with np.load(depth) as d:
                    mask = d["mask"].astype(np.float32) * (1.0 / 255.0)
                    mask_pixels = np.repeat(mask[..., None], 4, axis=2)
                    mask_pixels[..., 3] = 1.0
                    self.depth_in.apply_recorded(scene, d["key"], d["co"], d["face_valid"].astype(np.float32),
                                                 mask_pixels, json.loads(str(d["meta"])))
        except Exception as exc:
            self.last_error = str(exc)


def render_folder(path):
    return os.path.join(path, "render")


def render_video(path):
    return os.path.join(path, "render.mp4")


def blend_label():
    """書き出しの控えに付ける名前 (開いている .blend の名前。保存していなければ untitled)"""
    name = os.path.splitext(os.path.basename(bpy.data.filepath))[0] if bpy.data.filepath else "untitled"
    return "".join("_" if c in '\\/:*?"<>|' else c for c in name) or "untitled"


def keep_render(path, video):
    """render.mp4 (render/) を render_<.blend の名前>.mp4 (render_<名前>/) に写して残す。
    render.mp4 は次の書き出しで上書きされるので、背景 (.blend) ごとの書き出しはこちらに残る。写した先を返す。"""
    if video:
        source = render_video(path)
        target = os.path.join(path, "render_%s.mp4" % blend_label())
        shutil.copyfile(source, target)
    else:
        source = render_folder(path)
        target = os.path.join(path, "render_%s" % blend_label())
        if os.path.isdir(target):
            shutil.rmtree(target)
        shutil.copytree(source, target)
    return target


def list_frames(folder):
    if not os.path.isdir(folder):
        return []
    return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                  if n.lower().endswith((".png", ".jpg", ".jpeg")))
