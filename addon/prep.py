"""テイクから、参照 (リファレンス) を入れられる動画生成モデルに渡す中間ファイル (生成用の素材) を書き出す。

特定のモデルには寄せない。「参照の動画 (カメラの動き・構図) と参照の画像 (人・場所) を受けて、文章で指示できる
動画生成モデル」に、そのまま渡せる形にする。生成そのものはしない (モデルへの送り方は利用者が決める)。

    <テイク>/for_generation/
      reference.mp4    参照の動画。テイクのカメラの動きで、背景のシーンを灰色の粘土 1 色で描き、人は実写から取った
                       白い輪郭線だけ (人の中は黒)。人物のマスク (アルファ / 深度のマスク) が無ければ背景だけ
      actor.jpg        役者の写真。範囲の実写から、人が大きく・くっきり・明るさの合ったコマを選び、人の外を灰色に
      layout.png       場所のレイアウト。人の立ち位置から 前 (カメラの奥) / 右 / 後ろ (カメラの方) / 左 を見た 2×2
      first_frame.png  最初のコマの実写 (最初のコマから作るモデル向け)。実写が無ければ参照の動画の最初のコマ
      camera.json      フレームごとのカメラ (Blender のワールド、Z 上、m) と画角
      prompt.txt       プロンプトのひな形 (<Video 1> <Picture 1> <Picture 2> の指し方の例)
      manifest.json    各ファイルの意味と、モデルの入力への当てはめ方

前の for_generation/ があれば消さずに for_generation_<日時>/ へ名前を変えて残す。
画像の縮小は Blender に入っている OpenImageIO、輪郭線と写真の選び方は numpy だけで行う (OpenCV は使わない)。
"""

import json
import math
import os
import tempfile
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

from . import take
try:
    from .i18n import _t
except ImportError:  # アドオンの外から単独で読まれたとき (render_clay.py・GTN のノードなど)。訳さない
    def _t(message):
        return message

FOLDER = "for_generation"
# 書き出しに入れないもの (ライブ・再生の板や深度スクリーン)
HIDE_NAMES = ("trkAR NDI Screen", "trkAR Camera Screen", "trkAR Depth Screen", "trkAR Depth Background")
CLAY_COLOR = (0.62, 0.62, 0.62)
# 輪郭線 (Canny) のしきい値。Sobel の |gx| + |gy| (画素 0〜1) で、ffmpeg の edgedetect と同じ 0.08 / 0.22
EDGE_LOW, EDGE_HIGH = 0.08, 0.22
LAYOUT_VIEW = (640, 360)
LAYOUT_FOV = 90.0
EYE_HEIGHT = 1.5
# レイアウトで隠す「立ち位置にある物」の大きさの上限 (横、m)。これより大きい物 (部屋・カウンター) は隠さない
LAYOUT_SMALL = 2.0
VERSION = 1


def output_size(render, long_side):
    """シーンの出力の縦横比のまま、長い辺を long_side に (16 の倍数)"""
    width = render.resolution_x * render.resolution_percentage / 100.0
    height = render.resolution_y * render.resolution_percentage / 100.0
    scale = long_side / max(width, height)

    def snap(v):
        return max(16, int(round(v * scale / 16.0)) * 16)

    return snap(width), snap(height)


# ---- 画像 ----

def _load(path, size=None):
    """画像を (h, w, c) float32 0〜1 (上が先頭) で。size = (w, h) なら OpenImageIO で縮める"""
    import OpenImageIO as oiio

    buf = oiio.ImageBuf(path)
    if size is not None and (buf.spec().width, buf.spec().height) != tuple(size):
        buf = oiio.ImageBufAlgo.resize(buf, roi=oiio.ROI(0, size[0], 0, size[1], 0, 1, 0, buf.nchannels))
    pixels = np.asarray(buf.get_pixels(oiio.FLOAT), dtype=np.float32)
    if pixels.ndim == 2:
        pixels = pixels[..., None]
    return pixels


def _resize_nearest(a, w, h):
    ys = np.minimum((np.arange(h) + 0.5) * a.shape[0] / h, a.shape[0] - 1).astype(int)
    xs = np.minimum((np.arange(w) + 0.5) * a.shape[1] / w, a.shape[1] - 1).astype(int)
    return a[ys][:, xs]


def _gray(rgb):
    return rgb[..., 0] * 0.299 + rgb[..., 1] * 0.587 + rgb[..., 2] * 0.114


def _dilate(mask, passes):
    out = mask.copy()
    for _ in range(passes):
        grown = out.copy()
        grown[1:] |= out[:-1]
        grown[:-1] |= out[1:]
        grown[:, 1:] |= out[:, :-1]
        grown[:, :-1] |= out[:, 1:]
        out = grown
    return out


# ffmpeg の edgedetect と同じ 5×5 のガウス (σ ≈ 1.4、合計 159)
_GAUSS = ((2, 4, 5, 4, 2), (4, 9, 12, 9, 4), (5, 12, 15, 12, 5), (4, 9, 12, 9, 4), (2, 4, 5, 4, 2))


def _blur5(g):
    """5×5 のガウス (端は端の値をのばす)"""
    h, w = g.shape
    p = np.pad(g, 2, mode="edge")
    out = np.zeros_like(g)
    for dy, row in enumerate(_GAUSS):
        for dx, weight in enumerate(row):
            out += weight * p[dy:dy + h, dx:dx + w]
    return out / 159.0


def edges(gray):
    """Canny の輪郭線 (bool)。ffmpeg の edgedetect と同じ手順: ぼかす → Sobel (|gx| + |gy|) → 勾配の向きの
    非極大値抑制で線を細くする → 強い線 (> EDGE_HIGH) と、強い線に隣り合う弱い線 (> EDGE_LOW) を残す"""
    g = np.pad(_blur5(gray.astype(np.float32)), 1, mode="edge")
    gx = (g[:-2, 2:] + 2 * g[1:-1, 2:] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[1:-1, :-2] + g[2:, :-2])
    gy = (g[2:, :-2] + 2 * g[2:, 1:-1] + g[2:, 2:]) - (g[:-2, :-2] + 2 * g[:-2, 1:-1] + g[:-2, 2:])
    mag = np.abs(gx) + np.abs(gy)
    # 勾配の向きを 4 つに分けて、その向きの両隣より大きいものだけ残す (画像は下へ y が増える)
    angle = (np.degrees(np.arctan2(gy, gx)) + 180.0) % 180.0
    m = np.pad(mag, 1)
    h, w = mag.shape
    def at(dy, dx):
        return m[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
    horizontal = (angle < 22.5) | (angle >= 157.5)
    down_right = (angle >= 22.5) & (angle < 67.5)
    vertical = (angle >= 67.5) & (angle < 112.5)
    down_left = (angle >= 112.5) & (angle < 157.5)
    keep = ((horizontal & (mag >= at(0, -1)) & (mag >= at(0, 1)))
            | (down_right & (mag >= at(1, 1)) & (mag >= at(-1, -1)))
            | (vertical & (mag >= at(-1, 0)) & (mag >= at(1, 0)))
            | (down_left & (mag >= at(1, -1)) & (mag >= at(-1, 1))))
    thin = np.where(keep, mag, 0.0)
    strong = thin > EDGE_HIGH
    weak = thin > EDGE_LOW
    sp = np.pad(strong, 1)
    near = np.zeros_like(strong)
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            near |= sp[dy:dy + h, dx:dx + w]
    return strong | (weak & near)


def _sharpness(gray, mask=None):
    lap = gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    if mask is not None:
        values = lap[mask[1:-1, 1:-1]]
        return float(values.var()) if values.size > 50 else 0.0
    return float(lap.var())


class Plate:
    """テイクの実写とマスクをフレームごとに読む (video/・alpha/・depth/ の mask)"""

    def __init__(self, path, info):
        self.path = path
        # 背景の CG とのずれ (コマ)。ここで受けるコマは CG の時間なので、ファイルはずれを引いたコマで読む
        self.offset = take.time_offset(info)
        self.has_video = bool(info.get("video", True)) and os.path.isdir(os.path.join(path, "video"))
        self.has_alpha = bool(info.get("alpha")) and os.path.isdir(os.path.join(path, "alpha"))
        self.has_depth = os.path.isdir(os.path.join(path, "depth")) and bool(os.listdir(os.path.join(path, "depth")))

    @property
    def has_mask(self):
        return self.has_alpha or self.has_depth

    def _file(self, sub, frame, ext):
        p = os.path.join(self.path, sub, take._frame_name(frame - self.offset) + ext)
        return p if os.path.exists(p) else None

    def color(self, frame, size=None):
        p = self._file("video", frame, ".jpg") if self.has_video else None
        return _load(p, size)[..., :3] if p else None

    def mask(self, frame, size):
        """人物のマスク (h, w) bool か None"""
        p = self._file("alpha", frame, ".png") if self.has_alpha else None
        if p:
            return _load(p, size)[..., 0] > 0.5
        p = self._file("depth", frame, ".npz") if self.has_depth else None
        if p:
            with np.load(p) as d:
                m = d["mask"][::-1]  # 深度のマスクは Blender の画像と同じ下から
            return _resize_nearest(m, size[0], size[1]) > 127
        return None


# ---- 書き出し ----

class _State:
    """書き出しのあいだ変えるシーンの設定を覚えて戻す"""

    def __init__(self, scene):
        r, shading = scene.render, scene.display.shading
        self.scene = scene
        self.render = {"engine": r.engine, "filepath": r.filepath, "x": r.resolution_x, "y": r.resolution_y,
                       "pct": r.resolution_percentage, "film": r.film_transparent}
        self.image = {n: getattr(r.image_settings, n) for n in ("media_type", "file_format", "color_mode")
                      if hasattr(r.image_settings, n)}
        self.shading = {n: getattr(shading, n) for n in ("light", "color_type", "single_color", "show_cavity",
                                                         "show_shadows", "show_object_outline")}
        self.aa = scene.display.render_aa
        self.camera = scene.camera
        self.frame = scene.frame_current
        self.hidden = {}
        for name in HIDE_NAMES:
            obj = bpy.data.objects.get(name)
            if obj is not None:
                self.hidden[name] = obj.hide_render
                obj.hide_render = True

    def restore(self):
        scene, r = self.scene, self.scene.render
        r.engine = self.render["engine"]
        r.filepath = self.render["filepath"]
        r.resolution_x, r.resolution_y, r.resolution_percentage = self.render["x"], self.render["y"], self.render["pct"]
        r.film_transparent = self.render["film"]
        for name, value in self.image.items():
            try:
                setattr(r.image_settings, name, value)
            except (TypeError, ValueError):
                pass
        for name, value in self.shading.items():
            try:
                setattr(scene.display.shading, name, value)
            except (TypeError, ValueError):
                pass
        scene.display.render_aa = self.aa
        scene.camera = self.camera
        for name, value in self.hidden.items():
            obj = bpy.data.objects.get(name)
            if obj is not None:
                obj.hide_render = value
        scene.frame_set(self.frame)


def _workbench(scene, size, color_type):
    r = scene.render
    r.engine = "BLENDER_WORKBENCH"
    r.resolution_x, r.resolution_y = size
    r.resolution_percentage = 100
    r.film_transparent = False
    if hasattr(r.image_settings, "media_type"):
        r.image_settings.media_type = "IMAGE"
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGB"
    shading = scene.display.shading
    shading.light = "STUDIO"
    shading.color_type = color_type
    shading.single_color = CLAY_COLOR
    shading.show_cavity = True
    shading.show_shadows = False
    shading.show_object_outline = False
    scene.display.render_aa = "8"


def _render_still(scene, path):
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True, scene=scene.name)


def _camera_entry(camera, frame):
    world = camera.matrix_world
    loc, rot, _scale = world.decompose()
    euler = rot.to_euler("XYZ")
    data = camera.data
    return {"frame": frame, "position": [round(v, 5) for v in loc],
            "rotation_euler_deg": [round(math.degrees(v), 4) for v in euler],
            "quaternion": [round(v, 6) for v in rot],
            "hfov_deg": round(math.degrees(data.angle_x), 4) if camera.type == "CAMERA" else None,
            "lens_mm": round(data.lens, 4) if camera.type == "CAMERA" else None,
            "sensor_width_mm": round(data.sensor_width, 4) if camera.type == "CAMERA" else None}


def encode_mp4(frames, fps, size, out):
    """PNG の連番を Blender のシーケンサーで MP4 (H.264) にする"""
    tmp = bpy.data.scenes.new("trkAR prep encode")
    try:
        editor = tmp.sequence_editor_create()
        strips = editor.strips if hasattr(editor, "strips") else editor.sequences
        strip = strips.new_image(name="reference", filepath=frames[0], channel=1, frame_start=1)
        for path in frames[1:]:
            strip.elements.append(os.path.basename(path))
        r = tmp.render
        r.resolution_x, r.resolution_y = size
        r.resolution_percentage = 100
        r.fps, r.fps_base = (int(round(fps)), 1.0) if abs(fps - round(fps)) < 1e-3 else (int(math.ceil(fps)),
                                                                                         math.ceil(fps) / fps)
        tmp.frame_start, tmp.frame_end = 1, len(frames)
        if hasattr(r.image_settings, "media_type"):
            r.image_settings.media_type = "VIDEO"
        r.image_settings.file_format = "FFMPEG"
        r.ffmpeg.format = "MPEG4"
        r.ffmpeg.codec = "H264"
        r.ffmpeg.constant_rate_factor = "HIGH"
        r.ffmpeg.audio_codec = "NONE"
        r.use_sequencer = True
        r.filepath = out
        bpy.ops.render.render(animation=True, scene=tmp.name)
    finally:
        bpy.data.scenes.remove(tmp)


def _reference_frames(plate, clay_paths, frames, size, work, progress):
    """粘土のコマに人 (中は黒、輪郭は白) を重ねた参照のコマを書く。パスの一覧を返す"""
    out = []
    for i, (frame, clay_path) in enumerate(zip(frames, clay_paths)):
        clay = _load(clay_path)[..., :3]
        mask = plate.mask(frame, size) if plate.has_mask else None
        if mask is not None:
            color = plate.color(frame, size)
            region = _dilate(mask, 2)
            clay[mask] = 0.0
            if color is not None:
                clay[edges(_gray(color)) & region] = 1.0
        path = os.path.join(work, "ref_%06d.png" % i)
        take.write_image(path, (np.clip(clay, 0, 1) * 255 + 0.5).astype(np.uint8))
        out.append(path)
        progress("reference", i + 1, len(frames))
    return out


def pick_actor(plate, frames, out_path):
    """人が大きく・くっきり・明るさが合ったコマを選んで、人の外を灰色にした写真を書く。選んだフレームか None"""
    if not plate.has_video:
        return None
    step = max(1, len(frames) // 60)
    best, best_score = None, -1.0
    for frame in frames[::step]:
        color = plate.color(frame, (640, 360))
        if color is None:
            continue
        gray = _gray(color)
        mask = plate.mask(frame, (640, 360)) if plate.has_mask else None
        if mask is not None:
            area = float(mask.mean())
            if area < 0.01:
                continue
            exposure = 1.0 - min(1.0, abs(float(gray[mask].mean()) - 0.5) * 1.6)
            score = min(area, 0.4) * _sharpness(gray, mask) * max(exposure, 0.05)
        else:
            score = _sharpness(gray)
        if score > best_score:
            best, best_score = frame, score
    if best is None:
        return None
    path = plate._file("video", best, ".jpg")
    color = _load(path)[..., :3]
    h, w = color.shape[:2]
    mask = plate.mask(best, (w, h)) if plate.has_mask else None
    if mask is not None and mask.any():
        region = _dilate(mask, 3)
        color[~region] = 0.5
        ys, xs = np.nonzero(region)
        pad_y, pad_x = int((ys.max() - ys.min()) * 0.15) + 8, int((xs.max() - xs.min()) * 0.15) + 8
        y0, y1 = max(0, ys.min() - pad_y), min(h, ys.max() + pad_y)
        x0, x1 = max(0, xs.min() - pad_x), min(w, xs.max() + pad_x)
        color = color[y0:y1, x0:x1]
    take.write_image(out_path, (np.clip(color, 0, 1) * 255 + 0.5).astype(np.uint8))
    return best


def person_matrix(player):
    """人の立ち位置 (床、+Y はテイクの始めにカメラが見ていた向き)。置き場があればその位置"""
    anchor = bpy.data.objects.get(take.ANCHOR_NAME)
    return anchor.matrix_world.copy() if anchor is not None else player.pivot.copy()


def _standing_in(scene, eye):
    """立ち位置 (eye) を囲む小さな物 (人の代わりのマネキンなど)。部屋全体のような大きな物は入れない"""
    found = []
    for obj in scene.objects:
        if obj.type not in {"MESH", "CURVE", "SURFACE", "META", "FONT"} or obj.hide_render:
            continue
        corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        xs, ys, zs = [c.x for c in corners], [c.y for c in corners], [c.z for c in corners]
        if max(max(xs) - min(xs), max(ys) - min(ys)) > LAYOUT_SMALL:
            continue
        pad = 0.15
        if min(xs) - pad <= eye.x <= max(xs) + pad and min(ys) - pad <= eye.y <= max(ys) + pad \
                and min(zs) <= eye.z + 0.3 and max(zs) >= eye.z - 1.2:
            found.append(obj)
    return found


def render_layout(scene, player, work, out_path):
    """人の立ち位置から 前 / 右 / 後ろ / 左 を見た 4 枚を 2×2 にする。立ち位置にある小さな物は隠す"""
    place = person_matrix(player)
    heading = math.atan2(place.col[1][1], place.col[1][0]) - math.pi / 2.0
    eye = place.translation + Vector((0.0, 0.0, EYE_HEIGHT))
    data = bpy.data.cameras.new("trkAR layout")
    data.sensor_fit = "HORIZONTAL"
    data.angle = math.radians(LAYOUT_FOV)
    data.clip_start = 0.2
    cam = bpy.data.objects.new("trkAR layout", data)
    scene.collection.objects.link(cam)
    hidden = [(obj, obj.hide_render) for obj in _standing_in(scene, eye)]
    for obj, _was in hidden:
        obj.hide_render = True
    try:
        _workbench(scene, LAYOUT_VIEW, "TEXTURE")
        scene.camera = cam
        views = []
        for i, turn in enumerate((0.0, -90.0, 180.0, 90.0)):  # 前 (カメラの奥)、右、後ろ (カメラの方)、左
            yaw = heading + math.radians(turn)
            cam.matrix_world = Matrix.Translation(eye) @ Matrix.Rotation(yaw, 4, "Z") \
                @ Matrix.Rotation(math.radians(90.0), 4, "X")
            path = os.path.join(work, "layout_%d.png" % i)
            _render_still(scene, path)
            views.append(_load(path)[..., :3])
        sheet = np.concatenate([np.concatenate(views[:2], axis=1), np.concatenate(views[2:], axis=1)], axis=0)
        take.write_image(out_path, (np.clip(sheet, 0, 1) * 255 + 0.5).astype(np.uint8))
    finally:
        for obj, was in hidden:
            obj.hide_render = was
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(data)


PROMPT = """\
<Video 1> is the camera move and the layout of the shot: the gray clay shapes are the set, and the white outline \
is the actor's position and pose. Keep the camera motion and the framing of <Video 1> exactly.
<Picture 1> is the location, seen from where the actor stands (top-left: forward, top-right: right, \
bottom-left: back toward the camera, bottom-right: left).
<Picture 2> is the actor's appearance.

[Write the shot here: who the actor is, what they do and feel, the time of day, the light and the mood.]
"""


def export(scene, player, camera, long_side=960, first=None, last=None, layout=True, progress=None):
    """再生中のテイク (player) から for_generation/ を書く。書いたフォルダと manifest (dict) を返す"""
    if not player.active:
        raise RuntimeError(_t("Make the take ready to play first"))
    progress = progress or (lambda *_args: None)
    info, path = player.info, player.path
    first = info["frame_start"] if first is None else first
    last = info["frame_end"] if last is None else last
    frames = list(range(first, last + 1))
    if not frames:
        raise RuntimeError(_t("No range to export"))
    out = os.path.join(path, FOLDER)
    if os.path.isdir(out):
        os.rename(out, out + time.strftime("_%Y%m%d_%H%M%S", time.localtime(os.path.getmtime(out))))
    os.makedirs(out)
    fps = scene.render.fps / scene.render.fps_base
    size = output_size(scene.render, long_side)
    plate = Plate(path, info)
    state = _State(scene)
    # 実写を毎コマ読み込む再生の処理は止める (書き出しには使わない。重い)
    handler = player._on_frame
    had_handler = handler in bpy.app.handlers.frame_change_pre
    if had_handler:
        bpy.app.handlers.frame_change_pre.remove(handler)
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="trkar_prep_") as work:
            _workbench(scene, size, "SINGLE")
            scene.camera = camera
            clay, cameras = [], []
            for i, frame in enumerate(frames):
                scene.frame_set(frame)
                cameras.append(_camera_entry(camera, frame))
                p = os.path.join(work, "clay_%06d.png" % i)
                _render_still(scene, p)
                clay.append(p)
                progress("clay", i + 1, len(frames))
            refs = _reference_frames(plate, clay, frames, size, work, progress)
            progress("encode", 0, 1)
            encode_mp4(refs, fps, size, os.path.join(out, "reference.mp4"))
            first_plate = plate.color(frames[0])
            take.write_image(os.path.join(out, "first_frame.png"),
                             (np.clip(first_plate if first_plate is not None else _load(refs[0])[..., :3], 0, 1)
                              * 255 + 0.5).astype(np.uint8))
            progress("actor", 0, 1)
            actor_frame = pick_actor(plate, frames, os.path.join(out, "actor.jpg"))
            if layout:
                progress("layout", 0, 1)
                render_layout(scene, player, work, os.path.join(out, "layout.png"))
    finally:
        state.restore()
        if had_handler and handler not in bpy.app.handlers.frame_change_pre:
            bpy.app.handlers.frame_change_pre.append(handler)

    with open(os.path.join(out, "camera.json"), "w", encoding="utf-8") as f:
        json.dump({"coordinate": "Blender world, Z up, meters. rotation XYZ euler (degrees) and quaternion (w, x, y, z)",
                   "fps": fps, "frame_start": first, "frame_end": last, "frames": cameras}, f, indent=1)
    with open(os.path.join(out, "prompt.txt"), "w", encoding="utf-8") as f:
        f.write(PROMPT)
    files = {
        "reference.mp4": {"role": "reference video (camera move and layout)", "input": "reference_video",
                          "width": size[0], "height": size[1], "fps": fps, "frames": len(frames),
                          "person": "outline" if plate.has_mask else "none (no person mask in the take)"},
        "actor.jpg": {"role": "reference image of the actor", "input": "reference_image",
                      "take_frame": actor_frame - plate.offset} if actor_frame is not None else None,
        "layout.png": {"role": "reference image of the location (2x2: forward, right, back, left from the actor)",
                       "input": "reference_image"} if layout else None,
        "first_frame.png": {"role": "first frame of the take (for models that start from an image)",
                            "input": "first_frame_image", "source": "plate" if plate.has_video else "reference"},
        "camera.json": {"role": "camera per frame (for tools that take camera paths)", "input": "camera_path"},
        "prompt.txt": {"role": "prompt template that refers to the files above", "input": "text"},
    }
    manifest = {
        "version": VERSION,
        "made_by": "trkAR Camera",
        "take": os.path.basename(path),
        "frame_start": first, "frame_end": last, "fps": fps, "seconds": round(len(frames) / fps, 3),
        # frame_start / frame_end は背景の CG の時間。テイクのコマはこれから time_offset を引いたもの
        "time_offset": plate.offset,
        "blend": take.blend_label(),
        "model": "Any video generation model that accepts a reference video and reference images with a text "
                 "prompt. <Video 1> = reference.mp4, <Picture 1> = layout.png, <Picture 2> = actor.jpg",
        "files": {k: v for k, v in files.items() if v is not None},
        "seconds_to_make": round(time.monotonic() - started, 1),
    }
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return out, manifest
