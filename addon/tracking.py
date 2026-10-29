"""ARKit 座標系のカメラ姿勢を Blender のカメラへ反映する。

ARKit : 右手系 / Y 上 / -Z 前方。camera.eulerAngles は roll(Z) → pitch(X) →
        yaw(Y) の順で適用される (R = Ry · Rx · Rz)。
Blender: 右手系 / Z 上。カメラのローカル軸 (-Z 前方 / Y 上) は ARKit と同じなので、
        ワールド軸だけ X 軸まわり +90° 回せば一致する: (x, y, z) → (x, -z, y)。

姿勢 (OSC) は映像 (NDI) より先に届くので、実写を NDI 受信しているときは、表示中の映像と
同じ撮影時刻の姿勢を履歴から補間して使う (「映像に合わせる」)。どちらにも iPhone の
ARFrame.timestamp (同じ時計) が付いているので、PC と iPhone の時計を合わせる必要はない。
"""

import bisect
import collections
import math

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from .osc import OSCServer

AXIS_CONVERSION = Matrix.Rotation(math.radians(90.0), 4, "X")

server = OSCServer()
_state = {"serial": 0, "location": None, "rotation": None, "raw": None, "anchor": None, "rig": None,
          "controls": None, "latest": None, "key": None, "sunk": None}
# True を返す間はカメラへ書き込まない (テイクの再生・書き出し中。アクションとぶつかって震えるため)
hold = None
# iPhone の REC ボタンの指示 (client_id, 開始か) を受けたときにメインスレッドから呼ぶ
rec_hook = None
# 実写の NDIInput (__init__ が入れる)。表示中の timecode を見て同じ時刻の姿勢を選ぶ
video = None
# テイクの録画中 (take.Recorder) に、届いた姿勢を 1 件ずつ Blender のカメラの姿勢にして渡す先。
# callable(PoseSample)。録画はタイムラインの進みでキーを打たず、止めたあとに映像の撮影時刻で組み立てる
sink = None
# 姿勢と映像の時刻がこれ以上離れていたら時計が違う (古い trkAR、iPhone 以外のソースなど) とみなして揃えない (秒)。
# 履歴 (osc.HISTORY、8 秒以上) より長くして、それより短い遅れは TOO_LATE として区別する
SYNC_LIMIT = 10.0
# 映像の遅れを測る件数 (30fps で 3 秒分)
LAG_SAMPLES = 90
# state: None (揃えない設定) / "SYNCED" / "NO_VIDEO" (実写を受けていない) /
#        "NO_TIME" (時刻が無い・合わない) / "TOO_LATE" (映像の遅れが履歴より大きい)
# lag: 映像が姿勢よりどれだけ遅れて届いているか (ms)。新しい映像が出るたびに 1 件
sync = {"state": None, "lag": collections.deque(maxlen=LAG_SAMPLES), "timecode": None}


class PoseSample:
    """録画用の姿勢 1 件。frame_time は ARFrame.timestamp (秒、映像の timecode と同じ時計) か None、
    received_at は PC に届いた時刻 (time.monotonic)。location / rotation はワールド座標で、
    基準・オフセット・Blender 側のずれまで足したもの (ライブでカメラに書くのと同じ。スムージングは除く)。
    lens は (レンズ mm, 被写界深度を使うか, F 値, フォーカス距離, レンズにキーを打つか, 被写界深度にキーを打つか)"""

    __slots__ = ("frame_time", "received_at", "location", "rotation", "lens")

    def __init__(self, frame_time, received_at, location, rotation, lens):
        self.frame_time = frame_time
        self.received_at = received_at
        self.location = location
        self.rotation = rotation
        self.lens = lens


class Anchor:
    """相対モードの基準。base = 基準にした Blender カメラの姿勢、ref = そのとき iPhone がいた姿勢。

    iPhone の姿勢 cur に対するカメラ姿勢は
        位置 = base_loc + Y · (cur_loc − ref_loc) · scale
        回転 = Y · cur_rot
    Y は iPhone とカメラの向き (ヨー) の差。Blender カメラから借りるのは位置と方位だけで、
    チルト・ロールは iPhone の実際の傾き (重力基準) のまま使う。実写と水平が合い、
    基準を取り直しても Blender カメラの傾きや天地の反転が持ち越されない。
    iPhone で前に歩けばカメラも自分の向いている方向へ進み、上下はワールド Z のまま。
    """

    def __init__(self, camera):
        loc, rot, _scale = camera.matrix_world.decompose()
        # 今のカメラに Blender 側のオフセットが乗っていれば、外した姿勢を基準にする
        loc, rot = remove_controls(_state["controls"], loc, rot)
        self.base_loc = loc
        self.base_rot = rot
        self.ref_loc = None
        self.ref_rot = None
        self.yaw = Quaternion()

    def set_reference(self, location, rotation):
        self.ref_loc = location.copy()
        self.ref_rot = rotation.copy()
        angle = _heading(self.base_rot) - _heading(rotation)
        self.yaw = Quaternion((0.0, 0.0, 1.0), angle)

    def apply(self, scale, location, rotation):
        loc = self.base_loc + self.yaw @ ((location - self.ref_loc) * scale)
        rot = self.yaw @ rotation
        return loc, rot


def _heading(rotation):
    """カメラの向いている方位角 (ワールド Z まわり)。真下/真上を向いているときは画面上方向で代用。"""
    forward = rotation @ Vector((0.0, 0.0, -1.0))
    if forward.x * forward.x + forward.y * forward.y < 1e-4:
        forward = rotation @ Vector((0.0, 1.0, 0.0))
    return math.atan2(forward.y, forward.x)


def reanchor(camera):
    """今のカメラ位置を基準にし直す。次に届いた iPhone の姿勢がゼロ点になる。"""
    _state["anchor"] = Anchor(camera) if camera is not None else None
    _state.pop("target", None)
    reset_smoothing()


def is_anchored():
    anchor = _state["anchor"]
    return anchor is not None and anchor.ref_loc is not None


def arkit_to_blender(pose, rig=None):
    """ARKit の pose を Blender ワールドの (位置, 回転) に変換する (オフセット適用前)。

    rig (`/client/rig`) があれば、iPhone のジョイスティックで動かした分を ARKit 系のまま先に合成する:
        位置 = rig 位置 + Ry(yaw) · 実機位置
        回転 = Ry(yaw) · 実機回転 · Rx(tilt) · Rz(roll)
    """
    rx, ry, rz = (math.radians(v) for v in pose.euler_deg)
    arkit_rot = Euler((rx, ry, rz), "ZXY").to_matrix().to_4x4()
    position = Vector(pose.position)
    if rig is not None:
        yaw = Matrix.Rotation(math.radians(rig.yaw_deg), 4, "Y")
        position = Vector(rig.position) + yaw.to_3x3() @ position
        arkit_rot = (
            yaw
            @ arkit_rot
            @ Matrix.Rotation(math.radians(rig.tilt_deg), 4, "X")
            @ Matrix.Rotation(math.radians(rig.roll_deg), 4, "Z")
        )
    arkit_matrix = Matrix.Translation(position) @ arkit_rot
    blender_matrix = AXIS_CONVERSION @ arkit_matrix
    return blender_matrix.to_translation(), blender_matrix.to_quaternion()


def apply_offsets(settings, location, rotation):
    yaw = Matrix.Rotation(math.radians(settings.yaw_offset), 3, "Z")
    location = yaw @ (location * settings.scale) + Vector(settings.location_offset)
    rotation = yaw.to_quaternion() @ rotation
    if settings.roll_offset:
        # ローカル Z (視線軸) まわり。縦持ち運用などで画像の向きを合わせる
        rotation = rotation @ Quaternion((0.0, 0.0, 1.0), math.radians(settings.roll_offset))
    return location, rotation


def apply_controls(settings, location, rotation):
    """Blender 側のオフセット (パネルの「Blender でオフセット」)。基準・スムージングの後に足す。

        位置 = 位置 + オフセット (ワールド座標)
        回転 = Rz(パン) · 回転 · Rx(チルト) · Rz(ロール)
    パンはカメラ位置を軸にワールド Z まわり、チルト / ロールはカメラのローカル軸まわり。
    シーンのプロパティなのでキーフレームを打てる。
    """
    _state["controls"] = (tuple(settings.ctl_location), settings.ctl_pan, settings.ctl_tilt, settings.ctl_roll)
    location = location + Vector(settings.ctl_location)
    if settings.ctl_pan:
        rotation = Quaternion((0.0, 0.0, 1.0), math.radians(settings.ctl_pan)) @ rotation
    if settings.ctl_tilt:
        rotation = rotation @ Quaternion((1.0, 0.0, 0.0), math.radians(settings.ctl_tilt))
    if settings.ctl_roll:
        rotation = rotation @ Quaternion((0.0, 0.0, 1.0), math.radians(settings.ctl_roll))
    return location, rotation


def remove_controls(controls, location, rotation):
    """apply_controls の逆。controls は最後にカメラへ書き込んだときの (移動, パン, チルト, ロール)。"""
    if controls is None:
        return location, rotation
    offset, pan_deg, tilt_deg, roll_deg = controls
    location = location - Vector(offset)
    local = (Quaternion((1.0, 0.0, 0.0), math.radians(tilt_deg))
             @ Quaternion((0.0, 0.0, 1.0), math.radians(roll_deg)))
    pan = Quaternion((0.0, 0.0, 1.0), math.radians(pan_deg))
    return location, pan.inverted() @ rotation @ local.inverted()


def last_raw_location():
    return _state["raw"]


def current_pose():
    """今追従している iPhone の最新の姿勢 (Client ID などの絞り込みを通ったもの)。受信していなければ None。"""
    return _state.get("latest") if server.running else None


def reset_smoothing():
    _state["location"] = None
    _state["rotation"] = None


def _accept(settings, pose):
    if settings.camera_id_filter >= 0 and pose.camera_id != settings.camera_id_filter:
        return False
    if settings.client_id_filter and pose.client_id != settings.client_id_filter:
        return False
    return True


def video_lag():
    """映像が姿勢よりどれだけ遅れて届いているか (ms)。(中央値, 小さい側 5%, 大きい側 95%) か None。"""
    samples = sorted(sync["lag"])
    if len(samples) < 5:
        return None
    last = len(samples) - 1
    return tuple(samples[int(q * last + 0.5)] for q in (0.5, 0.05, 0.95))


def _measure(latest, shown):
    """新しい映像が出たら、届いている最新の姿勢との撮影時刻の差を 1 件記録する。"""
    if shown is None or latest.frame_time is None or shown == sync["timecode"]:
        return
    sync["timecode"] = shown
    lag = latest.frame_time - shown / 10_000_000
    if abs(lag) <= SYNC_LIMIT:
        sync["lag"].append(lag * 1000.0)


def _bracket(latest, target):
    """撮影時刻 target (秒) を挟む 2 件の姿勢と補間の割合 (p0, p1, t)。揃えられなければ state の文字列。"""
    if latest.frame_time is None:
        return "NO_TIME"
    history = server.history(latest.client_id)
    if not history or abs(history[-1].frame_time - target) > SYNC_LIMIT:
        return "NO_TIME"
    # UDP は稀に順番が入れ替わるので撮影時刻で並べ直す
    history.sort(key=lambda pose: pose.frame_time)
    times = [pose.frame_time for pose in history]
    if target < times[0]:
        return "TOO_LATE"
    index = bisect.bisect_right(times, target)
    if index >= len(history):
        # 映像の方が新しい (姿勢が遅れて届いた): 最新の姿勢のまま
        return history[-1], history[-1], 0.0
    p0, p1 = history[index - 1], history[index]
    span = p1.frame_time - p0.frame_time
    return p0, p1, (target - p0.frame_time) / span if span > 0 else 1.0


def _sample(settings, latest, shown, rig):
    """カメラに当てる (位置, 回転, 水平 FOV)。shown (表示中の映像の timecode) があれば、
    その撮影時刻の姿勢を前後 2 件から補間する。揃えられないときは最新の姿勢。"""
    found = "NO_VIDEO"
    if shown is not None:
        found = _bracket(latest, shown / 10_000_000 - settings.pose_sync_offset / 1000.0)
    if isinstance(found, str):
        sync["state"] = found
        location, rotation = arkit_to_blender(latest, rig)
        return location, rotation, latest.hfov_deg
    sync["state"] = "SYNCED"
    p0, p1, t = found
    loc0, rot0 = arkit_to_blender(p0, rig)
    loc1, rot1 = arkit_to_blender(p1, rig)
    return loc0.lerp(loc1, t), rot0.slerp(rot1, t), p0.hfov_deg + (p1.hfov_deg - p0.hfov_deg) * t


def _place(settings, location, rotation, floor_set=False):
    """iPhone の姿勢 (Blender のワールド座標) を基準・オフセットで置いた姿勢。相対で基準が無ければ None。

    floor_set は iPhone で床が決まっているか (/client/current の floorHeight が 0 でない)。決まっていれば
    iPhone の高さは床からの高さなので、相対でも「高さは iPhone の床から」なら高さだけは床の高さ + その値にする
    (水平の位置と向きは Blender のカメラから動いた分)。実写の床と Blender の床が合う"""
    if settings.mode == "RELATIVE":
        anchor = _state["anchor"]
        if anchor is None or anchor.ref_loc is None:
            return None
        placed, turned = anchor.apply(settings.scale, location, rotation)
        if floor_set and settings.floor_from_iphone:
            placed.z = settings.floor_z + location.z * settings.scale
        return placed, turned
    return apply_offsets(settings, location, rotation)


def _update_target(settings, camera, latest, shown):
    rig = server.rig_for(latest.client_id) if settings.use_rig else None
    if rig is not None and rig.camera_id != latest.camera_id:
        rig = None
    _state["rig"] = rig
    location, rotation, hfov = _sample(settings, latest, shown, rig)
    _state["raw"] = location.copy()
    if settings.mode == "RELATIVE":
        anchor = _state["anchor"]
        if anchor is None and camera is not None:
            anchor = _state["anchor"] = Anchor(camera)
        if anchor is not None and anchor.ref_loc is None:
            anchor.set_reference(location, rotation)
    target = _place(settings, location, rotation, bool(latest.floor_height))
    if target is not None:
        _state["target"] = target
    _state["hfov"] = hfov


def _feed_sink(settings, camera, latest):
    """前回から届いた姿勢を全部、録画の sink へ渡す (ライブでカメラに書くのと同じ変換、スムージングなし)。
    レンズ・被写界深度はこの tick でカメラに当てた値を使う (姿勢より変化がずっと遅いので)。"""
    rig = _state.get("rig")
    lens = None
    if camera.type == "CAMERA":
        cam = camera.data
        lens_rig = rig if settings.use_rig_lens else None
        lens = (cam.lens, cam.dof.use_dof, cam.dof.aperture_fstop, cam.dof.focus_distance,
                bool(settings.sync_fov or lens_rig is not None or settings.ctl_lens > 0.0), lens_rig is not None)
    poses = server.history(latest.client_id) if latest.frame_time is not None else [latest]
    last = _state["sunk"]
    for pose in poses:
        stamp = pose.frame_time if pose.frame_time is not None else pose.received_at
        if last is not None and stamp <= last:
            continue
        location, rotation = arkit_to_blender(pose, rig)
        placed = _place(settings, location, rotation, bool(pose.floor_height))
        if placed is None:
            continue
        location, rotation = apply_controls(settings, *placed)
        sink(PoseSample(pose.frame_time, pose.received_at, location, rotation, lens))
        _state["sunk"] = stamp


def tick():
    """メインスレッドのタイマー。姿勢 (映像に合わせるなら表示中の映像と同じ時刻のもの) をカメラへ書き込む。"""
    if not server.running:
        return None
    scene = bpy.context.scene
    if scene is None:
        return 1.0 / 60.0
    settings = scene.trkar
    camera = settings.camera or scene.camera
    for client_id, start in server.take_rec_commands():
        # 受信側の設定で受け付けない / 別の iPhone (Client ID 指定あり) の指示は無視する
        if rec_hook is None or not settings.rec_from_remote:
            continue
        if settings.client_id_filter and client_id != settings.client_id_filter:
            continue
        try:
            rec_hook(start)
        except Exception as exc:  # noqa: BLE001  録画の失敗でカメラ追従まで止めない
            server.last_error = "REC: %s: %s" % (type(exc).__name__, exc)
    serial, pose = server.latest()

    if pose is not None and serial != _state["serial"]:
        _state["serial"] = serial
        if _accept(settings, pose):
            settings.last_client_id = pose.client_id
            _state["latest"] = pose

    latest = _state.get("latest")
    shown = video.shown_timecode if video is not None and video.running else None
    if latest is not None:
        _measure(latest, shown)
        if not settings.pose_sync:
            shown = None
        # 新しい姿勢が届くか、映像を合わせる場合は表示中の映像が変わったら取り直す
        key = (_state["serial"], shown, settings.pose_sync_offset if shown is not None else 0.0)
        if key != _state["key"]:
            _state["key"] = key
            _update_target(settings, camera, latest, shown)
    if not settings.pose_sync:
        sync["state"] = None

    if hold is not None and hold():
        # 受信は続け、再開したときは最新の姿勢へすぐ合わせる
        reset_smoothing()
        return 1.0 / 60.0

    target = _state.get("target")
    if camera is not None and target is not None:
        location, rotation = target
        smoothing = settings.smoothing
        if smoothing > 0.0 and _state["location"] is not None:
            factor = 1.0 - smoothing
            location = _state["location"].lerp(location, factor)
            rotation = _state["rotation"].slerp(rotation, factor)
        _state["location"] = location
        _state["rotation"] = rotation
        # Blender 側のオフセットはスムージングの外で毎回足す (スライダーやキーフレームがすぐ効く)
        location, rotation = apply_controls(settings, location, rotation)

        rot_matrix = rotation.to_matrix().to_4x4()
        camera.matrix_world = Matrix.Translation(location) @ rot_matrix

        rig = _state.get("rig") if settings.use_rig_lens else None
        if camera.type == "CAMERA":
            _apply_lens(settings, camera.data, rig, _state.get("hfov", 0.0))

        if sink is not None and latest is not None:
            _feed_sink(settings, camera, latest)
        else:
            _state["sunk"] = None

        if settings.record and bpy.context.screen is not None and bpy.context.screen.is_animation_playing:
            frame = scene.frame_current
            camera.keyframe_insert("location", frame=frame)
            camera.keyframe_insert(_rotation_path(camera), frame=frame)
            if camera.type == "CAMERA":
                if settings.sync_fov or rig is not None or settings.ctl_lens > 0.0:
                    camera.data.keyframe_insert("lens", frame=frame)
                if rig is not None:
                    dof = camera.data.dof
                    dof.keyframe_insert("use_dof", frame=frame)
                    dof.keyframe_insert("aperture_fstop", frame=frame)
                    dof.keyframe_insert("focus_distance", frame=frame)

    return 1.0 / 60.0


def _apply_lens(settings, cam, rig, hfov):
    """画角と被写界深度。Blender 側の焦点距離 > iPhone のレンズ設定 (rig) > 画角の同期 の順に優先する。"""
    if settings.ctl_lens > 0.0:
        cam.lens_unit = "MILLIMETERS"
        cam.sensor_fit = "HORIZONTAL"
        cam.sensor_width = 36.0
        cam.lens = settings.ctl_lens
    elif rig is not None and rig.focal_mm > 0.0:
        # iPhone 側の焦点距離は 35mm 判 (横 36mm) 換算
        cam.lens_unit = "MILLIMETERS"
        cam.sensor_fit = "HORIZONTAL"
        cam.sensor_width = 36.0
        cam.lens = rig.focal_mm
    elif settings.sync_fov and hfov > 0.0:
        cam.lens_unit = "FOV"
        cam.sensor_fit = "HORIZONTAL"
        cam.angle = math.radians(hfov)

    if rig is not None:
        dof = cam.dof
        dof.use_dof = rig.fstop > 0.0
        if rig.fstop > 0.0:
            dof.focus_object = None
            dof.aperture_fstop = rig.fstop
            # 実寸 (m) で届くので移動量と同じスケールを掛ける
            dof.focus_distance = max(0.01, rig.focus_distance * settings.scale)


def _rotation_path(obj):
    if obj.rotation_mode == "QUATERNION":
        return "rotation_quaternion"
    if obj.rotation_mode == "AXIS_ANGLE":
        return "rotation_axis_angle"
    return "rotation_euler"


def start(port, camera=None):
    server.start(port)
    _state["serial"] = 0
    _state["latest"] = None
    _state["key"] = None
    _state["sunk"] = None
    sync["state"] = None
    sync["timecode"] = None
    sync["lag"].clear()
    reanchor(camera)
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, persistent=True)


def stop():
    server.stop()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
