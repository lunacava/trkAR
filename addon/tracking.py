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
import time

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from . import perf
from .osc import FOCUS_ADDRESS, OSCServer

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
# 深度の DepthInput (__init__ が入れる)。カメラより先に新しい形を出す
depth = None
# モニターの端末からのリモート操作 (/client/remote) がこれ以上来なければ止める (秒)
REMOTE_TIMEOUT = 0.5
# Blender のビューのコントローラー (view_controller.Controller)。倒している間、受信中ならここで
# 「Blender でオフセット」に積算する (受信していないときは、コントローラーがカメラを直接動かす)
local_stick = None
# コントローラーへ今のピント (/trkar/focus) を返す間隔 (秒)
FOCUS_FEEDBACK_INTERVAL = 0.1
# 被写界深度のピントを自動で合わせる距離 (Blender の長さ) を返す関数 (__init__ が入れる)。None を返せば合わせない
focus_hook = None
# タイマーの間隔。1/60 を頼んでも Blender では 1 秒に 42 回ほどしか呼ばれず、60fps の描画のうち同じカメラのまま
# 描かれるコマが混ざった (コマ落ち)。細かくして描画ごとに必ず更新する (1 回 0.2ms ほどなので重くならない)
INTERVAL = 1.0 / 240.0
# テイクの録画中 (take.Recorder) に、届いた姿勢を 1 件ずつ Blender のカメラの姿勢にして渡す先。
# callable(PoseSample)。録画はタイムラインの進みでキーを打たず、止めたあとに映像の撮影時刻で組み立てる
sink = None
# 姿勢と映像の時刻がこれ以上離れていたら時計が違う (古い trkAR、iPhone 以外のソースなど) とみなして揃えない (秒)。
# 履歴 (osc.HISTORY、8 秒以上) より長くして、それより短い遅れは TOO_LATE として区別する
SYNC_LIMIT = 10.0
# 映像の遅れを測る件数 (30fps で 3 秒分)
LAG_SAMPLES = 90
# 表示中の映像より新しい姿勢がまだ届いていないとき (Wi-Fi で姿勢が一瞬止まってからまとめて届くなど)、
# 直前の動きから先を予測する上限 (秒)。それより先は、上限のところで止める。
# 予測しないと、そのあいだ CG のカメラだけが古い姿勢で止まり、姿勢が届いた瞬間に飛ぶ (パン中の「パカッ」)
PREDICT_LIMIT = 0.2
# 予測に使う速さを測る幅 (秒)。隣り合う 2 件 (60Hz で 17ms) だと ARKit の細かな揺れまで伸ばしてしまう
PREDICT_BASE = 0.05
# 予測した回数を数える期間 (秒)
PREDICT_WINDOW = 5.0
# 映像と姿勢が同じ ARFrame のとき、映像の timecode (100ns に丸め) が姿勢の撮影時刻よりわずかに新しく見える。
# これより小さい差は同じ時刻として扱う (秒)
SAME_FRAME = 0.001
# state: None (揃えない設定) / "SYNCED" / "NO_VIDEO" (実写を受けていない) /
#        "NO_TIME" (時刻が無い・合わない) / "TOO_LATE" (映像の遅れが履歴より大きい)
# lag: 映像が姿勢よりどれだけ遅れて届いているか (ms)。新しい映像が出るたびに 1 件
# predicted: 姿勢が届いていなかった映像のコマ (time.monotonic, 最新の姿勢から何 ms 先か)。直近 PREDICT_WINDOW 秒
sync = {"state": None, "lag": collections.deque(maxlen=LAG_SAMPLES), "timecode": None,
        "predicted": collections.deque(), "predicted_timecode": None, "predicted_total": 0}


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

        位置 = オフセット + パンの補正 + Rz(パン) · 位置
        回転 = Rz(パン) · 回転 · Rx(チルト) · Rz(ロール)
    パンは iPhone の動きごとワールド Z まわりに回す (iPhone のコントローラーのパン CameraRig と同じ)。
    パンした後に iPhone で前に進むと、パンした後の画面の前に進む。パンを変えた瞬間にカメラが跳ばないよう、
    パンの補正 (ctl_pan_shift) を sync_pan_shift で合わせる。チルト / ロールはカメラのローカル軸まわり。
    シーンのプロパティなのでキーフレームを打てる。
    """
    shift = Vector(settings.ctl_pan_shift)
    _state["controls"] = (tuple(settings.ctl_location), settings.ctl_pan, settings.ctl_tilt, settings.ctl_roll,
                          tuple(shift))
    pan = Quaternion((0.0, 0.0, 1.0), math.radians(settings.ctl_pan))
    return Vector(settings.ctl_location) + shift + pan @ location, _controlled_rotation(settings, rotation)


def sync_pan_shift(settings, location):
    """パン (ctl_pan) が前に当てたときから変わっていたら、今のカメラの位置を軸に回るように
    パンの補正を足す (Rz(前のパン) · 位置 − Rz(今のパン) · 位置)。location はオフセットを足す前の今の位置"""
    pan = settings.ctl_pan
    last = _state.get("pan_applied")
    _state["pan_applied"] = pan
    if last is None or last == pan:
        return
    before = Quaternion((0.0, 0.0, 1.0), math.radians(last)) @ location
    after = Quaternion((0.0, 0.0, 1.0), math.radians(pan)) @ location
    settings.ctl_pan_shift = Vector(settings.ctl_pan_shift) + before - after


def reset_pan_shift(settings):
    """オフセットを全部戻すとき (パンの補正も 0 にし、戻したパンで補正を足さない)。"""
    settings.ctl_pan_shift = (0.0, 0.0, 0.0)
    _state["pan_applied"] = settings.ctl_pan


def _controlled_rotation(settings, rotation):
    """apply_controls の回転の分 (パン・チルト・ロール)。"""
    if settings.ctl_pan:
        rotation = Quaternion((0.0, 0.0, 1.0), math.radians(settings.ctl_pan)) @ rotation
    if settings.ctl_tilt:
        rotation = rotation @ Quaternion((1.0, 0.0, 0.0), math.radians(settings.ctl_tilt))
    if settings.ctl_roll:
        rotation = rotation @ Quaternion((0.0, 0.0, 1.0), math.radians(settings.ctl_roll))
    return rotation


def remove_controls(controls, location, rotation):
    """apply_controls の逆。controls は最後にカメラへ書き込んだときの (移動, パン, チルト, ロール, パンの補正)。"""
    if controls is None:
        return location, rotation
    offset, pan_deg, tilt_deg, roll_deg, shift = controls
    local = (Quaternion((1.0, 0.0, 0.0), math.radians(tilt_deg))
             @ Quaternion((0.0, 0.0, 1.0), math.radians(roll_deg)))
    pan = Quaternion((0.0, 0.0, 1.0), math.radians(pan_deg)).inverted()
    return pan @ (location - Vector(offset) - Vector(shift)), pan @ rotation @ local.inverted()


def last_raw_location():
    return _state["raw"]


def pose_rate():
    """追従している iPhone から姿勢が届いている周期 (Hz)。直近 1 秒の撮影時刻で数える。1 秒以上届いていなければ 0"""
    latest = _state.get("latest") if server.running else None
    if latest is None or time.monotonic() - latest.received_at > 1.0:
        return 0.0
    if latest.frame_time is None:
        return 0.0
    times = [p.frame_time for p in server.history(latest.client_id) if p.frame_time is not None]
    recent = [t for t in times if t > times[-1] - 1.0] if times else []
    if len(recent) < 2 or recent[-1] <= recent[0]:
        return 0.0
    return (len(recent) - 1) / (recent[-1] - recent[0])


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


def predicted_recent():
    """直近 PREDICT_WINDOW 秒に予測した映像のコマの (数, いちばん先まで予測した ms)。"""
    recent = sync["predicted"]
    now = time.monotonic()
    while recent and now - recent[0][0] > PREDICT_WINDOW:
        recent.popleft()
    return len(recent), max((ahead for _, ahead in recent), default=0.0)


def _count_prediction(target, ahead):
    """姿勢が届いていなかった映像のコマを 1 件数える (同じコマで何度も取り直しても 1 回)。ahead は秒"""
    if target == sync["predicted_timecode"]:
        return
    sync["predicted_timecode"] = target
    sync["predicted"].append((time.monotonic(), ahead * 1000.0))
    sync["predicted_total"] += 1


def _bracket(latest, target, predict=False):
    """撮影時刻 target (秒) を挟む 2 件の姿勢と補間の割合 (p0, p1, t)。揃えられなければ state の文字列。
    target が最新の姿勢より新しいとき、predict なら直前の動きを伸ばす (t > 1)。しなければ最新の姿勢のまま"""
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
    if index >= len(history) and target - times[-1] < SAME_FRAME:
        # 最新の姿勢が表示中の映像と同じ ARFrame (丸めの差だけ)
        return history[-1], history[-1], 0.0
    if index >= len(history):
        # 映像の方が新しい (姿勢が遅れて届いた)
        last = history[-1]
        _count_prediction(target, target - last.frame_time)
        if not predict or len(history) < 2:
            return last, last, 0.0
        # PREDICT_BASE 秒ほど前の姿勢との差を速さにして、target まで (上限 PREDICT_LIMIT) 伸ばす
        base = max(0, bisect.bisect_right(times, last.frame_time - PREDICT_BASE) - 1)
        first = history[base]
        span = last.frame_time - first.frame_time
        if span <= 0:
            return last, last, 0.0
        ahead = min(target - last.frame_time, PREDICT_LIMIT)
        return first, last, (last.frame_time + ahead - first.frame_time) / span
    p0, p1 = history[index - 1], history[index]
    span = p1.frame_time - p0.frame_time
    return p0, p1, (target - p0.frame_time) / span if span > 0 else 1.0


def _sample(settings, latest, shown, rig):
    """カメラに当てる (位置, 回転, 水平 FOV)。shown (表示中の映像の timecode) があれば、
    その撮影時刻の姿勢を前後 2 件から補間する。揃えられないときは最新の姿勢。"""
    found = "NO_VIDEO"
    if shown is not None:
        found = _bracket(latest, shown / 10_000_000 - settings.pose_sync_offset / 1000.0, settings.pose_predict)
    if isinstance(found, str):
        sync["state"] = found
        location, rotation = arkit_to_blender(latest, rig)
        return location, rotation, latest.hfov_deg
    sync["state"] = "SYNCED"
    p0, p1, t = found
    loc0, rot0 = arkit_to_blender(p0, rig)
    loc1, rot1 = arkit_to_blender(p1, rig)
    hfov = p0.hfov_deg + (p1.hfov_deg - p0.hfov_deg) * min(t, 1.0)
    if t <= 1.0:
        return loc0.lerp(loc1, t), rot0.slerp(rot1, t), hfov
    # 予測 (最新の姿勢より先): 位置は同じ速さで、回転は p0 → p1 の回転を同じ割合で伸ばす
    # (Quaternion.slerp は 0〜1 の外を受け付けない)
    turn = rot1 @ rot0.inverted()
    if turn.w < 0.0:
        turn.negate()  # 短い向きに回す
    axis, angle = turn.to_axis_angle()
    return loc0.lerp(loc1, t), Quaternion(axis, angle * t) @ rot0, hfov


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
        lens_rig = _state.get("lens_src")
        lens = (cam.lens, cam.dof.use_dof, cam.dof.aperture_fstop, cam.dof.focus_distance,
                bool(settings.sync_fov or lens_rig is not None or settings.ctl_lens > 0.0),
                lens_rig is not None or _autofocus(settings))
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
    """tick の本体の時間を測る (perf)。"""
    started = time.perf_counter()
    try:
        return _tick()
    finally:
        perf.add("tracking", time.perf_counter() - started)


def _flush_inputs():
    """カメラより先に、届いている実写と深度の新しいコマを出す。実写・深度・カメラが別々のタイマーだと
    呼ばれる順が毎回変わり、実写を新しいコマにしたのにカメラは前のコマの時刻のまま描かれることがあった (ふわふわ)。
    ここで実写 → 深度 → カメラの順にそろえる (それぞれのタイマーも残るが、出すものが無ければ何もしない)"""
    for source in (video, depth):
        if source is None or not source.running:
            continue
        try:
            source._tick_body()
        except Exception as exc:  # noqa: BLE001  実写・深度の失敗でカメラ追従まで止めない
            server.last_error = "%s: %s" % (type(exc).__name__, exc)


def _tick():
    """メインスレッドのタイマー。姿勢 (映像に合わせるなら表示中の映像と同じ時刻のもの) をカメラへ書き込む。"""
    if not server.running:
        return None
    scene = bpy.context.scene
    if scene is None:
        return INTERVAL
    _flush_inputs()
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
        return INTERVAL

    remote = _active_remote(settings, latest)
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
        if remote is not None:
            # 別の端末からのスティックを「Blender でオフセット」に積算する (このカメラの今の向きで)
            _apply_remote(settings, rotation, remote)
        _apply_local_stick(settings, rotation)
        sync_pan_shift(settings, location)
        # Blender 側のオフセットはスムージングの外で毎回足す (スライダーやキーフレームがすぐ効く)
        location, rotation = apply_controls(settings, location, rotation)

        rot_matrix = rotation.to_matrix().to_4x4()
        camera.matrix_world = Matrix.Translation(location) @ rot_matrix

        # レンズ: リモート操作が来ていればそのレンズ、無ければこの端末のコントローラーのレンズ設定
        rig = remote if remote is not None else (_state.get("rig") if settings.use_rig_lens else None)
        _state["lens_src"] = rig
        _sync_autofocus(settings, rig, (_state.get("rig"), remote))
        _sync_lens_overrides(settings, rig)
        if camera.type == "CAMERA":
            apply_lens(settings, camera.data, rig, _state.get("hfov", 0.0))

        _send_focus(settings, camera, (latest, remote))

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
                if rig is not None or _autofocus(settings):
                    dof = camera.data.dof
                    dof.keyframe_insert("use_dof", frame=frame)
                    dof.keyframe_insert("aperture_fstop", frame=frame)
                    dof.keyframe_insert("focus_distance", frame=frame)

    return INTERVAL


def apply_lens(settings, cam, rig=None, hfov=0.0):
    """画角と被写界深度。Blender 側 (Blender でオフセットの焦点距離・F 値・ピント) > iPhone のレンズ設定 (rig) >
    画角の同期 / カメラ自体の設定 の順に優先し、最後にオートフォーカスを当てる。受信していないときは、
    ビューのコントローラーが rig なしで呼ぶ"""
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

    # Blender 側の F 値 (0 = 上書きしない、負 = 被写界深度を切る) とピント距離 (0 = 上書きしない)
    if settings.ctl_fstop < 0.0:
        cam.dof.use_dof = False
    elif settings.ctl_fstop > 0.0:
        cam.dof.use_dof = True
        cam.dof.focus_object = None
        cam.dof.aperture_fstop = settings.ctl_fstop
    if settings.ctl_focus > 0.0:
        cam.dof.focus_object = None
        cam.dof.focus_distance = settings.ctl_focus

    # オートフォーカス (実写の板の距離)。F 値と被写界深度のオン・オフは上のまま
    distance = focus_hook() if focus_hook is not None else None
    if distance is not None:
        cam.dof.focus_object = None
        cam.dof.focus_distance = max(0.01, distance)


def _active_remote(settings, latest):
    """今追っているカメラ (latest の Camera ID) を動かすリモート操作のうち、REMOTE_TIMEOUT 秒以内に届いたもの。"""
    if latest is None or not settings.use_remote:
        _state["remote_at"] = None
        return None
    remote = server.remote_for(latest.camera_id)
    if remote is None or time.monotonic() - remote.received_at > REMOTE_TIMEOUT:
        _state["remote_at"] = None
        return None
    return remote


def remote_status():
    """リモート操作中なら、操作している端末の Client ID。していなければ None。"""
    latest = current_pose()
    if latest is None or _state.get("remote_at") is None:
        return None
    remote = server.remote_for(latest.camera_id)
    return remote.client_id if remote is not None else None


def _apply_remote(settings, rotation, remote):
    """リモートのスティックを、前回からの時間分「Blender でオフセット」(ctl_*) に積算する。
    rotation はオフセットを足す前のカメラの向き (iPhone の姿勢)。移動は、これにオフセットの向きを足した
    (これから書く) カメラの水平の向きが前で、上下はワールド Z。パンはカメラ位置を軸に、チルト・ロールはカメラの軸まわり
    (trkAR のコントローラー CameraRig.step と同じ動き。速さは移動量のスケールも掛ける)。
    カメラのオブジェクトの向きは読まない (タイムラインの再生中は、テイクのアニメーションが書き戻した向きになり、
    パンしても前が変わらなかった)。ALL RST / ROT RST の回数が変わったらオフセットを戻す"""
    now = time.monotonic()
    last = _state.get("remote_at")
    _state["remote_at"] = now
    resets = _state.setdefault("remote_resets", {})
    seen = resets.get(remote.client_id)
    resets[remote.client_id] = (remote.pose_reset, remote.rotation_reset)
    if seen is not None and remote.pose_reset != seen[0]:
        settings.ctl_location = (0.0, 0.0, 0.0)
        settings.ctl_pan = settings.ctl_tilt = settings.ctl_roll = 0.0
        reset_pan_shift(settings)
    elif seen is not None and remote.rotation_reset != seen[1]:
        settings.ctl_pan = settings.ctl_tilt = settings.ctl_roll = 0.0
    if last is None:
        return
    integrate_controls(settings, rotation, remote, min(now - last, 0.1))


def _apply_local_stick(settings, rotation):
    """Blender のビューのコントローラーを倒している間、前回からの時間分を積算する。"""
    now = time.monotonic()
    last = _state.get("local_at")
    stick = local_stick
    if stick is None or not stick.active:
        _state["local_at"] = None
        return
    _state["local_at"] = now
    if last is not None:
        integrate_controls(settings, rotation, stick, min(now - last, 0.1))


def integrate_controls(settings, rotation, stick, dt):
    """スティック (stick.move / crane / look / roll と、倒し切ったときの move_speed m/s・rotate_speed 度/s) を
    dt 秒分「Blender でオフセット」に積算する (リモートと、Blender のビューのコントローラーで共通)。"""
    (move_x, move_y), crane = stick.move, stick.crane
    (pan, tilt), roll = stick.look, stick.roll
    if move_x or move_y or crane:
        rotation = _controlled_rotation(settings, rotation)
        forward = rotation @ Vector((0.0, 0.0, -1.0))
        forward.z = 0.0
        if forward.length < 1e-3:
            # 真下 / 真上を向いているときは画面の上方向を前とみなす
            forward = rotation @ Vector((0.0, 1.0, 0.0))
            forward.z = 0.0
        forward = forward.normalized() if forward.length > 1e-6 else Vector((0.0, 1.0, 0.0))
        right = Vector((forward.y, -forward.x, 0.0))
        step = stick.move_speed * dt * settings.scale
        settings.ctl_location = Vector(settings.ctl_location) + (
            right * move_x + forward * move_y + Vector((0.0, 0.0, crane))) * step
    turn = stick.rotate_speed * dt
    if pan:
        settings.ctl_pan = _wrap_degrees(settings.ctl_pan - pan * turn)
    if tilt:
        settings.ctl_tilt = max(-89.0, min(89.0, settings.ctl_tilt + tilt * turn))
    if roll:
        settings.ctl_roll = _wrap_degrees(settings.ctl_roll + roll * turn)


def _sync_autofocus(settings, lens_src, sources):
    """コントローラーの LENS の AF (オートフォーカス) を「ピントを人の板に合わせる」に当てる。
    AF を切り替えた・ピントを触った回数 (af_serial) が変わったときだけ当てるので、Blender のパネルのチェックも使える。
    回数は、レンズを使っていないコントローラーの分も覚える (/trkar/focus で受け取った回数として返す)"""
    seen = _state.setdefault("af_serials", {})
    for source in sources:
        if source is None or getattr(source, "af_serial", None) is None:
            continue
        last = seen.get(source.client_id)
        seen[source.client_id] = source.af_serial
        if source is lens_src and last is not None and last != source.af_serial:
            settings.dof_autofocus = source.auto_focus
            if not source.auto_focus:
                settings.ctl_focus = 0.0  # コントローラーでピントを触った: Blender 側のピントの上書きを外す


def _sync_lens_overrides(settings, lens_src):
    """最後に触った方のレンズが効くようにする。コントローラー (iPad の CTL やリモートの iPhone の LENS) で焦点距離や
    F 値を変えたら、Blender 側の上書き (Blender でオフセットの焦点距離・F 値。Blender のビューのコントローラーで入る) を
    外して、コントローラーの値にする。コントローラーが同じ値を送り続けている間は Blender 側の上書きのまま"""
    if lens_src is None:
        return
    seen = _state.setdefault("lens_seen", {})
    now = (round(lens_src.focal_mm, 2), round(lens_src.fstop, 2))
    last = seen.get(lens_src.client_id)
    seen[lens_src.client_id] = now
    if last is None:
        return
    if now[0] != last[0] and settings.ctl_lens:
        settings.ctl_lens = 0.0
    if now[1] != last[1] and settings.ctl_fstop:
        settings.ctl_fstop = 0.0


def _send_focus(settings, camera, sources):
    """今のピント (オートフォーカスの状態とピント距離) を、コントローラー (姿勢を送ってくる端末と、リモートの端末) に返す。"""
    if camera.type != "CAMERA":
        return
    now = time.monotonic()
    if now - _state.get("focus_sent_at", 0.0) < FOCUS_FEEDBACK_INTERVAL:
        return
    _state["focus_sent_at"] = now
    latest = sources[0]
    if latest is None:
        return
    seen = _state.get("af_serials", {})
    distance = camera.data.dof.focus_distance / max(settings.scale, 1e-6)
    for source in sources:
        if source is None:
            continue
        host = server.address_for(source.client_id)
        server.send(host, settings.port, FOCUS_ADDRESS,
                    [latest.camera_id, int(settings.dof_autofocus), float(distance), seen.get(source.client_id, -1)])


def _wrap_degrees(value):
    return (value + 180.0) % 360.0 - 180.0


def _autofocus(settings):
    """被写界深度 (F 値・ピント) を毎回当てているか (録画でキーを打つか)。名前はオートフォーカスのときから"""
    return (focus_hook is not None and settings.dof_autofocus) or settings.ctl_fstop != 0.0 or settings.ctl_focus > 0.0


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
    sync["predicted"].clear()
    sync["predicted_timecode"] = None
    sync["predicted_total"] = 0
    reanchor(camera)
    if not bpy.app.timers.is_registered(tick):
        bpy.app.timers.register(tick, persistent=True)
    perf.start()


def stop():
    server.stop()
    perf.stop()
    if bpy.app.timers.is_registered(tick):
        bpy.app.timers.unregister(tick)
