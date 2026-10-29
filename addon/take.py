"""録画 (REC) とテイクの再生。

REC 中はタイムラインを実時間で再生し、届いた実写 (アルファ込み)・深度スクリーンの形・カメラの姿勢を
撮影時刻 (iPhone の ARFrame.timestamp) つきで全部溜める。止めたら、テイクの各フレームに撮影時刻の
いちばん近い実写を 1 枚選び、カメラの動きはその実写の撮影時刻の姿勢でテイク用のアクションにキーを打つ
(plan_take / bake_keys)。テイクのフォルダには次を書く:

    <テイク>/take.json          fps・フレーム範囲・カメラ・アクション名と、撮影の来歴
                                (スレート・場所・撮影環境・撮った機種・製作者。shot_info)
    <テイク>/video/000001.jpg   実写の色
    <テイク>/alpha/000001.png   実写のアルファ (RVM の切り抜きを受けていたときだけ。白黒)
    <テイク>/depth/000001.npz   深度スクリーンの頂点・面・マスク
    <テイク>/render.mp4         書き出し (PNG 連番なら render/0001.png …)

テイク再生中は受信を止め、フレームが変わるたびに同じフレームの実写と深度を読み込む。
スクリーン類は "trkAR NDI" などライブと同じ画像を見ているので、そのまま再生・レンダーできる。
"""

import bisect
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

try:
    from . import tracking
except ImportError:  # アドオンの外で単独に読むとき (render_clay.py・クラウドの書き出し)。録画 (Recorder) は使わない
    tracking = None

# 再生中だけカメラをこの Empty の子にする。動かす・回すとテイクのカメラの軌跡ごと背景の中でずれる
ANCHOR_NAME = "trkAR テイクの置き場"

# 書き込み待ちがこれ以上たまったら届いた実写・深度を捨てる (そのフレームは前後の実写で埋まる)
MAX_IN_FLIGHT = 12
# 録画中はタイムラインの終わりをここまで延ばす (分)
MAX_MINUTES = 30
# 名前を ARtrk から変える前のテイクのフォルダ (2026-10-29 まで)。あればそのまま使う
LEGACY_TAKES = "artrk_takes"


def takes_folder(configured):
    """テイクを置くフォルダ。空なら .blend の隣の trkar_takes (保存していなければ ~/Movies/trkAR Takes、Windows は ~/Videos)。
    名前を ARtrk から変える前のフォルダ (artrk_takes、ARtrk Takes) があって新しい方が無ければ、そちらを使い続ける"""
    if not configured:
        configured = "//trkar_takes"
        if bpy.data.filepath and not os.path.isdir(bpy.path.abspath(configured)) \
                and os.path.isdir(bpy.path.abspath("//" + LEGACY_TAKES)):
            configured = "//" + LEGACY_TAKES
    if configured.startswith("//") and not bpy.data.filepath:
        base = "~/Videos/" if sys.platform == "win32" else "~/Movies/"
        default, legacy = os.path.expanduser(base + "trkAR Takes"), os.path.expanduser(base + "ARtrk Takes")
        return legacy if os.path.isdir(legacy) and not os.path.isdir(default) else default
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
        "system": "trkAR" if pose is not None or client is not None else None,
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
    camera = scene.trkar.camera or scene.camera
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


def anchor_pivot(action, frame, distance, parent_world=None):
    """置き場の基準にするテイクの位置と向き (Matrix)。位置は始めのフレームでカメラの `distance` 前
    (人の居る所) を床 (Z=0) に下ろした所、+Y はそのときカメラが見ている向き (水平)。
    置き場をこれに重ねれば録ったときのまま、動かせばその分だけ、回せばここを中心に回る。
    テイクが違っても置き場の Y の矢印がカメラの見る向きになる。
    parent_world: 録ったときのカメラの親の空間 (親の matrix_world @ matrix_parent_inverse)。親が無ければ None"""
    basis = _action_matrix(action, frame)
    if parent_world is not None:
        basis = parent_world @ basis
    forward = basis.to_3x3() @ Vector((0.0, 0.0, -1.0))
    forward.z = 0.0
    if forward.length < 1e-4:
        forward = Vector((0.0, 1.0, 0.0))
    forward.normalize()
    point = basis.translation + forward * distance
    point.z = 0.0
    heading = math.atan2(forward.y, forward.x) - math.pi / 2.0
    return Matrix.Translation(point) @ Matrix.Rotation(heading, 4, "Z")


# ---- テイクの置き場 (背景のオフセット) はテイクごとに take.json の placement に残す ----
# 以前は .blend に 1 つだけの Empty の位置を全部のテイクで使っていたので、別のテイクで合わせた置き場に
# 新しいテイクが移され、録ったときと違う所からカメラが始まっていた。

def ensure_anchor(scene):
    anchor = bpy.data.objects.get(ANCHOR_NAME)
    if anchor is None:
        anchor = bpy.data.objects.new(ANCHOR_NAME, None)
        anchor.empty_display_type = "ARROWS"
        anchor.empty_display_size = 0.5
        scene.collection.objects.link(anchor)
    anchor.rotation_mode = "XYZ"
    return anchor


def placement_of(info):
    """take.json の placement: {"matrix": 置き場の matrix_world (4x4), "front_distance": 基準にした手前の距離}"""
    placement = info.get("placement")
    if not placement or "matrix" not in placement:
        return None
    return placement


def save_placement(path, placement):
    """take.json の placement を書き換える (None なら消す)。変わらなければ書かない"""
    try:
        info = load_info(path)
    except (OSError, ValueError):
        return
    if info.get("placement") == placement:
        return
    if placement is None:
        info.pop("placement", None)
    else:
        info["placement"] = placement
    with open(os.path.join(path, "take.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)


def _matrix_list(matrix):
    return [[round(v, 6) for v in row] for row in matrix]


def _same_matrix(a, b, tolerance=1e-4):
    return all(abs(a[i][j] - b[i][j]) <= tolerance for i in range(4) for j in range(4))


def parent_world_of(camera):
    """カメラの親の空間 (録ったアクションの座標の基準)。置き場の子になったまま保存された .blend
    (再生中に保存した) なら、元の親は分からないので親なしとみなす"""
    if camera.parent is None or camera.parent.name == ANCHOR_NAME:
        return Matrix()
    return camera.parent.matrix_world @ camera.matrix_parent_inverse


def place_take(scene, camera, action, info, distance):
    """再生・書き出し・クラウドのレンダーで、テイクを背景の中に置く (カメラを置き場の子にする)。
    置き場が take.json にあればそこへ、無ければ録ったときの位置 (置き場があっても基準の点に戻す)。
    返り値 (pivot, 使った手前の距離, 戻す前の置き場)。戻す前の置き場は、take.json に置き場が無いのに .blend の
    置き場が基準の点からずれていたとき、その matrix_world (前の作りで合わせた置き場を「前の置き場を使う」で戻せる)。
    カメラの親は呼ぶ前の状態を見るので、元の親は呼んだ側で控えておく"""
    placement = placement_of(info)
    if placement is not None:
        distance = placement.get("front_distance", distance)
    parent_world = parent_world_of(camera)
    pivot = anchor_pivot(action, info["frame_start"], distance, parent_world)
    anchor = bpy.data.objects.get(ANCHOR_NAME)
    previous = None
    if placement is not None:
        anchor = ensure_anchor(scene)
        anchor.matrix_world = Matrix(placement["matrix"])
    elif anchor is not None:
        if not _same_matrix(anchor.matrix_world, pivot):
            previous = anchor.matrix_world.copy()
        anchor.matrix_world = pivot
    if anchor is not None and anchor is not camera:
        camera.parent = anchor
        # 置き場が基準の点にあれば、録ったときの (元の親から見た) 位置と同じになる
        camera.matrix_parent_inverse = pivot.inverted() @ parent_world
    elif camera.parent is not None and camera.parent.name == ANCHOR_NAME:
        camera.parent = None
        camera.matrix_parent_inverse = Matrix()
    return pivot, distance, previous


# 録画中に届いた実写・深度を一旦置くフォルダ (テイクの中)。止めたらフレームの名前に付け替えて消す
STAGING = ".incoming"
# 映像・深度の撮影時刻と姿勢の撮影時刻の時計がこれ以上違えば別の時計 (iPhone 以外のソース、古いアプリ) とみなし、
# PC に届いた時刻で並べる (秒)
CLOCK_LIMIT = 10.0


class Entry:
    """録画中に届いた実写・深度 1 件。capture は撮影時刻 (秒) か None、arrival は届いた時刻 (time.monotonic)"""

    __slots__ = ("capture", "arrival", "name", "ok", "time")

    def __init__(self, capture, arrival, name):
        self.capture = capture
        self.arrival = arrival
        self.name = name
        self.ok = False
        self.time = None


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else None


def _clock_delta(pairs):
    """[(撮影時刻か None, 届いた時刻)] の (撮影時刻 − 届いた時刻) の中央値。撮影時刻が無ければ None"""
    return _median([c - a for c, a in pairs if c is not None])


def _place_on_clock(entries, reference):
    """entries の time を基準の時計 (姿勢の撮影時刻) で埋める。撮影時刻が同じ時計ならそのまま、
    違う時計か無ければ届いた時刻を基準の時計に直したもの。撮影時刻をそのまま使えたかを返す"""
    delta = _clock_delta([(e.capture, e.arrival) for e in entries])
    same = delta is not None and abs(delta - reference) < CLOCK_LIMIT
    for e in entries:
        e.time = e.capture if same and e.capture is not None else e.arrival + (delta if same else reference)
    return same


def _nearest(times, t):
    """昇順の times の中で t にいちばん近い添字"""
    i = bisect.bisect_left(times, t)
    if i <= 0:
        return 0
    if i >= len(times):
        return len(times) - 1
    return i if times[i] - t < t - times[i - 1] else i - 1


def _interpolate(samples, times, t):
    """姿勢 (PoseSample、times は同じ並びの時刻) の t での (位置, 回転, レンズ)。端は端の値"""
    i = bisect.bisect_right(times, t)
    if i <= 0:
        s = samples[0]
        return s.location, s.rotation, s.lens
    if i >= len(samples):
        s = samples[-1]
        return s.location, s.rotation, s.lens
    a, b = samples[i - 1], samples[i]
    span = times[i] - times[i - 1]
    f = (t - times[i - 1]) / span if span > 0 else 1.0
    return a.location.lerp(b.location, f), a.rotation.slerp(b.rotation, f), a.lens


def plan_take(fps, poses, video, depth, start, stop, pose_offset=0.0, depth_offset=0.0, smoothing=0.0):
    """録画中に溜めたものから、テイクのフレームごとの (実写の添字か None, 深度の添字か None, 位置, 回転, レンズ)
    と集計を作る。start / stop は (録り始め・止めたときに表示していた実写の撮影時刻か None, その瞬間の monotonic)。

    - 姿勢の撮影時刻 (ARFrame.timestamp) を基準の時計にする。映像・深度も同じ時計ならその撮影時刻を、
      違えば PC に届いた時刻をこの時計に直して使う
    - フレーム i の時刻は 録り始めの実写の撮影時刻 + i / fps。実写はその時刻にいちばん近い 1 枚
    - カメラはその実写の撮影時刻 − pose_offset の姿勢 (前後 2 件から補間)。実写が無ければフレームの時刻の姿勢
    - 深度はその実写の撮影時刻 − depth_offset 以前でいちばん新しいもの (ライブの「映像に合わせる」と同じ)
    """
    poses = [p for p in poses if p.location is not None]
    reference = _clock_delta([(p.frame_time, p.received_at) for p in poses])
    pose_capture = reference is not None
    if reference is None:
        reference = _clock_delta([(e.capture, e.arrival) for e in video]) or 0.0
    for p in poses:
        if p.frame_time is None:
            p.frame_time = p.received_at + reference
    poses.sort(key=lambda p: p.frame_time)
    if smoothing > 0.0 and poses:
        # ライブのスムージング (tick ごとに前の値へ寄せる) と同じことを届いた順に
        location, rotation = poses[0].location, poses[0].rotation
        for p in poses:
            location = location.lerp(p.location, 1.0 - smoothing)
            rotation = rotation.slerp(p.rotation, 1.0 - smoothing)
            p.location, p.rotation = location, rotation
    pose_times = [p.frame_time for p in poses]

    video = [e for e in video if e.ok]
    depth = [e for e in depth if e.ok]
    video_same = _place_on_clock(video, reference) if video else None
    depth_same = _place_on_clock(depth, reference) if depth else None
    video.sort(key=lambda e: e.time)
    depth.sort(key=lambda e: e.time)
    video_times = [e.time for e in video]
    depth_times = [e.time for e in depth]

    def moment(mark):
        """録り始め・止めた瞬間に見えていたものの時刻 (基準の時計)"""
        shown, mono = mark
        if video and video_same and shown is not None:
            return shown
        if video:
            return mono + (_clock_delta([(e.capture, e.arrival) for e in video]) if video_same else reference)
        return mono + reference

    t0, t1 = moment(start), moment(stop)
    last = video_times[-1] if video else (pose_times[-1] if poses else None)
    if last is not None:
        t1 = min(t1, last)
    count = max(1, int(math.floor((t1 - t0) * fps + 1e-6)) + 1)
    count = min(count, int(fps * 60 * MAX_MINUTES))

    frames, used, repeated, worst = [], set(), 0, 0.0
    previous = None
    for i in range(count):
        t = t0 + i / fps
        vi = di = None
        when = t
        if video:
            vi = _nearest(video_times, t)
            when = video_times[vi]
            worst = max(worst, abs(when - t))
            repeated += vi == previous
            previous = vi
            used.add(vi)
        if depth:
            target = when - depth_offset + 0.001
            di = max(0, bisect.bisect_right(depth_times, target) - 1)
        location = rotation = lens = None
        if poses:
            location, rotation, lens = _interpolate(poses, pose_times, when - pose_offset)
        frames.append((vi, di, location, rotation, lens))

    rate = None
    if len(pose_times) > 10:
        span = pose_times[-1] - pose_times[0]
        rate = round((len(pose_times) - 1) / span, 1) if span > 0 else None
    stats = {
        # capture_time: 実写と姿勢を撮影時刻で合わせた / arrival: PC に届いた時刻で並べた (時計が違う・古いアプリ)
        "method": "capture_time" if (video_same if video else pose_capture) else "arrival",
        "video_received": len(video),
        "video_repeated": repeated,
        "video_unused": len([1 for e in video if t0 - 0.5 / fps <= e.time <= t1 + 0.5 / fps]) - len(used)
        if video else 0,
        "video_max_error_ms": round(worst * 1000.0, 1),
        "depth_received": len(depth),
        "depth_capture_time": depth_same,
        "pose_rate_hz": rate,
        "pose_offset_ms": round(pose_offset * 1000.0, 1),
    }
    return frames, video, depth, stats


def _rotation_values(camera, rotation, previous):
    """回転 (Quaternion) をカメラの rotation_mode の値に。オイラーは前のフレームと連続になるように"""
    mode = camera.rotation_mode
    if mode == "QUATERNION":
        if previous is not None and rotation.dot(Quaternion(previous)) < 0.0:
            rotation = -rotation
        return "rotation_quaternion", tuple(rotation)
    if mode == "AXIS_ANGLE":
        axis, angle = rotation.to_axis_angle()
        return "rotation_axis_angle", (angle, axis.x, axis.y, axis.z)
    euler = rotation.to_euler(mode, Euler(previous, mode)) if previous is not None else rotation.to_euler(mode)
    return "rotation_euler", tuple(euler)


def _put_curves(fcurves, path, frames, columns):
    """frames と、同じ並びの値の列 (配列の要素ごと) を F カーブにまとめて入れる"""
    for index, values in enumerate(columns):
        fc = fcurves.find(path, index=index) or fcurves.new(path, index=index)
        fc.keyframe_points.clear()
        fc.keyframe_points.add(len(frames))
        co = [0.0] * (2 * len(frames))
        co[0::2] = frames
        co[1::2] = values
        fc.keyframe_points.foreach_set("co", co)
        fc.update()


def bake_keys(camera, action, lens_action, start_frame, frames):
    """plan_take のフレームごとの姿勢・レンズを、カメラ (オブジェクト) とレンズのアクションへ入れる。
    カメラに親があれば、親の今の位置から見た値にする (ライブで matrix_world に書いてキーを打つのと同じ)"""
    keyed = [(start_frame + i, f) for i, f in enumerate(frames) if f[2] is not None]
    if not keyed:
        return
    parent_inverse = None
    if camera.parent is not None:
        parent_inverse = (camera.parent.matrix_world @ camera.matrix_parent_inverse).inverted()
    numbers, locations, rotations = [], [], []
    path, previous = None, None
    for number, (_vi, _di, location, rotation, _lens) in keyed:
        world = Matrix.Translation(location) @ rotation.to_matrix().to_4x4()
        basis = parent_inverse @ world if parent_inverse is not None else world
        loc, rot, _scale = basis.decompose()
        path, previous = _rotation_values(camera, rot, previous)
        numbers.append(float(number))
        locations.append(tuple(loc))
        rotations.append(previous)
    fcurves = _new_fcurves(action, "OBJECT")
    _put_curves(fcurves, "location", numbers, list(zip(*locations)))
    _put_curves(fcurves, path, numbers, list(zip(*rotations)))

    lenses = [f[4] for _n, f in keyed]
    if camera.type != "CAMERA" or lens_action is None or not any(lens is not None for lens in lenses):
        return
    lenses = [lens or next(v for v in lenses if v is not None) for lens in lenses]
    lens_curves = _new_fcurves(lens_action, "CAMERA")
    if any(lens[4] for lens in lenses):
        _put_curves(lens_curves, "lens", numbers, [[lens[0] for lens in lenses]])
    if any(lens[5] for lens in lenses):
        _put_curves(lens_curves, "dof.use_dof", numbers, [[float(lens[1]) for lens in lenses]])
        _put_curves(lens_curves, "dof.aperture_fstop", numbers, [[lens[2] for lens in lenses]])
        _put_curves(lens_curves, "dof.focus_distance", numbers, [[lens[3] for lens in lenses]])


class Recorder:
    """REC。録画中はタイムラインを実時間で再生し (シーンのアニメーションも見え方もライブのまま)、
    届いた実写・深度・姿勢を撮影時刻つきで全部溜める。止めたら plan_take でテイクの各フレームに
    撮影時刻のいちばん近い実写を 1 枚選び、カメラのキーはその実写の撮影時刻の姿勢で打つ。
    実写と CG のカメラが必ず同じ瞬間になり、Blender が重くて再生のフレームが飛んでも欠けない。"""

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
        self.stats = {}
        self._recording = False
        self._pool = None
        self._in_flight = 0
        self._lock = threading.Lock()
        self._alpha = None
        self._video = []
        self._depth = []
        self._poses = []
        self._start_mark = (None, 0.0)
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

    def _shown_mark(self):
        """(今表示している実写の撮影時刻 (秒) か None, 今の monotonic)"""
        shown = self.ndi_in.shown_timecode if self.ndi_in.running else None
        return (shown / 10_000_000 if shown else None), time.monotonic()

    def start(self, context, folder, shot=None):
        """shot は take.json に足す撮影の来歴 (shot_info)。"""
        scene = context.scene
        settings = scene.trkar
        camera = _camera_of(scene)
        if camera is None:
            raise RuntimeError("録画するカメラがありません")
        name = time.strftime("take_%Y%m%d_%H%M%S")
        path = os.path.join(folder, name)
        for sub in ("video", "depth", "alpha", os.path.join(STAGING, "video"), os.path.join(STAGING, "alpha"),
                    os.path.join(STAGING, "depth")):
            os.makedirs(os.path.join(path, sub), exist_ok=True)

        # テイクごとに新しいアクションへ記録する (前のテイクや手で打ったキーは残る)
        action = bpy.data.actions.new("trkAR " + name)
        action.use_fake_user = True
        lens_action = bpy.data.actions.new("trkAR " + name + " Lens")
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
        self.stats = {}
        self._alpha = None
        self._video, self._depth, self._poses = [], [], []
        self._in_flight = 0
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="TrkARTake")
        self._info = {
            "name": name,
            "fps": scene.render.fps / scene.render.fps_base,
            "frame_start": self.start_frame,
            "camera": camera.name,
            "action": action.name,
            "lens_action": lens_action.name,
        }
        self._info.update(shot or {})

        # キーはタイムラインの進みでは打たない (止めたあとに撮影時刻で組み立てる)
        settings.record = False
        scene.frame_end = self.start_frame + int(self._info["fps"] * 60 * MAX_MINUTES)
        # 実時間に合わせる (重いときはフレームを飛ばす。テイクのフレームは撮影時刻で決めるので欠けない)
        scene.sync_mode = "FRAME_DROP"
        scene.frame_set(self.start_frame)
        self._recording = True
        self.started_at = time.monotonic()
        self._start_mark = self._shown_mark()
        self._stopped_playing_at = None
        self.ndi_in.frame_sink = self._on_video
        self.depth_in.frame_sink = self._on_depth
        tracking.sink = self._poses.append
        if not bpy.app.timers.is_registered(self._watch):
            bpy.app.timers.register(self._watch, first_interval=0.5)
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

    def _enqueue(self, entries, timecode, write, *args):
        """受信スレッドから。届いた 1 件を entries に足して .incoming/ への書き込みを頼む。
        書き込み待ちがたまりすぎていたら捨てる。止めたあと (_recording が False) は何もしない"""
        with self._lock:
            if not self._recording or self._pool is None:
                return None
            if self._in_flight >= MAX_IN_FLIGHT:
                self.dropped += 1
                return None
            capture = timecode / 10_000_000 if timecode and timecode > 0 else None
            entry = Entry(capture, time.monotonic(), _frame_name(len(entries) + 1))
            entries.append(entry)
            self._in_flight += 1
            self._pool.submit(write, entry, *args)
            return entry

    def _on_video(self, timecode, rgba, alpha):
        """NDI の受信スレッドから。届いた実写を全部書く"""
        if self._alpha is None:
            # 最初のフレームで決める (途中で RVM を付け外ししても拡張子は変えない)
            self._alpha = bool(alpha)
        if self._enqueue(self._video, timecode, self._write_video, rgba, self._alpha) is not None:
            self.frames = len(self._video)

    def _on_depth(self, timecode, state):
        """深度の受信スレッドから。処理した深度を全部書く"""
        self._enqueue(self._depth, timecode, self._write_depth, state)

    def _write_video(self, entry, rgba, alpha):
        try:
            # アルファ付きの PNG は 1080p で 1 枚 4〜5MB になるので、色は JPEG、アルファは白黒の PNG に分ける
            write_image(os.path.join(self.path, STAGING, "video", entry.name + ".jpg"), rgba[..., :3])
            if alpha:
                write_image(os.path.join(self.path, STAGING, "alpha", entry.name + ".png"), rgba[..., 3])
            entry.ok = True
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)
        finally:
            with self._lock:
                self._in_flight -= 1

    def _write_depth(self, entry, state):
        try:
            key, co, face_valid, mask_pixels, meta = state
            # マスクは RGB が同じ値 (A = 1) なので 1 チャンネル 8bit で持つ
            mask = (mask_pixels[..., 0] * 255.0 + 0.5).astype(np.uint8)
            np.savez_compressed(os.path.join(self.path, STAGING, "depth", entry.name + ".npz"), key=np.array(key),
                                co=co, face_valid=face_valid.astype(np.uint8), mask=mask, meta=json.dumps(meta))
            entry.ok = True
        except Exception as exc:
            self.errors += 1
            self.last_error = str(exc)
        finally:
            with self._lock:
                self._in_flight -= 1

    def stop(self, context):
        if not self._recording:
            return None
        stop_mark = self._shown_mark()
        with self._lock:
            self._recording = False
        self.ndi_in.frame_sink = None
        self.depth_in.frame_sink = None
        tracking.sink = None
        if context.screen is not None and context.screen.is_animation_playing:
            bpy.ops.screen.animation_cancel(restore_frame=False)
        self._pool.shutdown(wait=True)
        self._pool = None

        scene = context.scene
        restore = self._restore
        settings = scene.trkar
        settings.record = restore["record"]
        scene.sync_mode = restore["sync_mode"]
        fps = self._info["fps"]
        frames, video, depth, self.stats = plan_take(
            fps, self._poses, self._video, self._depth, self._start_mark, stop_mark,
            pose_offset=settings.pose_sync_offset / 1000.0 if settings.pose_sync else 0.0,
            depth_offset=settings.depth_sync_offset / 1000.0 if settings.depth_sync else 0.0,
            smoothing=settings.smoothing)
        end = self.start_frame + len(frames) - 1
        self.last_frame = end
        self.frames = len(frames)
        scene.frame_end = end
        self._link_frames(frames, video, depth)
        camera = bpy.data.objects.get(restore["camera"])
        if camera is not None:
            bake_keys(camera, self._action, self._lens_action, self.start_frame, frames)
            # ライブに戻る: カメラには録画前のアクションを戻す (テイクは「再生」で割り当てる)
            _assign(camera, restore["action"])
            _assign(camera.data, restore["lens_action"])
        has_video = bool(video)
        info = dict(self._info, frame_end=end, alpha=bool(self._alpha) and has_video, video=has_video,
                    frames=self.frames, dropped=self.dropped, sync=self.stats)
        with open(os.path.join(self.path, "take.json"), "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)
        # カメラの動き自体もテイクフォルダへ焼く。.blend を保存し忘れたり、別の .blend で
        # 開いたりしてもこのテイクだけで再生できるように (アクションは .blend の中にしかない)。
        _save_actions(self.path, self._action, self._lens_action)
        return self.path

    def _link_frames(self, frames, video, depth):
        """選んだ実写・深度を video/000001.jpg … の名前に付け替え、.incoming/ を片付ける
        (同じ 1 枚を続けて使うフレームはハードリンク、できなければコピー)"""
        staging = os.path.join(self.path, STAGING)
        streams = (("video", ".jpg", video, 0), ("depth", ".npz", depth, 1))
        if self._alpha:
            streams += (("alpha", ".png", video, 0),)
        for sub, ext, entries, column in streams:
            if not entries:
                continue
            for i, frame in enumerate(frames):
                index = frame[column]
                if index is None:
                    continue
                source = os.path.join(staging, sub, entries[index].name + ext)
                target = os.path.join(self.path, sub, _frame_name(self.start_frame + i) + ext)
                if not os.path.exists(source):
                    continue
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copyfile(source, target)
        shutil.rmtree(staging, ignore_errors=True)


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
        # 再生中のテイクの置き場の基準 (anchor_pivot) と、そのときの手前の距離
        self.pivot = Matrix()
        self.front_distance = 1.0
        # 置き場を基準の点に戻す前の位置 (前の作りで合わせた置き場。「前の置き場を使う」で戻す) と、そのテイク
        self.previous_anchor = None
        self.previous_for = ""

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
        # 置き場の子のまま保存された .blend (再生中に保存) なら、元の親は無いものとして戻す
        leftover = camera.parent is not None and camera.parent.name == ANCHOR_NAME
        self._restore = {
            "camera": camera.name,
            "action": _assign(camera, action),
            "lens_action": _assign(camera.data, lens_action) if lens_action is not None else None,
            "has_lens": lens_action is not None,
            "lock": scene.render.use_lock_interface,
            "parent": "" if leftover or camera.parent is None else camera.parent.name,
            "parent_inverse": Matrix() if leftover else camera.matrix_parent_inverse.copy(),
            "rotation_mode": camera.rotation_mode,
        }
        # 回転の F カーブと同じ回転モードにする (別の .blend のオイラーのカメラだと、
        # クォータニオンで録った向きが効かず位置しか動かない)
        paths = {fc.data_path for fc in _action_fcurves(action)}
        if "rotation_quaternion" in paths:
            camera.rotation_mode = "QUATERNION"
        elif "rotation_euler" in paths and camera.rotation_mode in ("QUATERNION", "AXIS_ANGLE"):
            camera.rotation_mode = "XYZ"
        # 置き場はテイクごと (take.json の placement)。無ければ録ったときの位置で再生する
        self.pivot, self.front_distance, previous = place_take(scene, camera, action, info,
                                                               scene.trkar.ndi_in_front_distance)
        # 同じテイクを再生し直したとき (置き場を作った・書き出した) は前の置き場を覚え直さない
        if previous is not None:
            self.previous_anchor = previous
        elif path != self.previous_for:
            self.previous_anchor = None
        self.previous_for = path
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

    def save_placement(self):
        """今の置き場をこのテイクの take.json に残す (基準の点のままなら消す = 録ったときの位置)"""
        if self.info is None or not self.path:
            return
        anchor = bpy.data.objects.get(ANCHOR_NAME)
        if anchor is None or _same_matrix(anchor.matrix_world, self.pivot):
            save_placement(self.path, None)
        else:
            save_placement(self.path, {"matrix": _matrix_list(anchor.matrix_world),
                                       "front_distance": self.front_distance})

    def stop(self, scene):
        if self._on_frame in bpy.app.handlers.frame_change_pre:
            bpy.app.handlers.frame_change_pre.remove(self._on_frame)
        self.save_placement()
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
