"""trkAR の `/client/current` と `/client/rig` (と `/client/rec`・`/client/info`) を受ける最小限の OSC (UDP) 受信機。

外部ライブラリに依存しないよう、OSCKit が送る型 (i / f / s / d / h / T / F)
だけを自前でデコードする。受信はワーカースレッドで行い、最新の 1 件と、
映像に合わせるための直近の履歴をロック付きで保持する (Blender の API はメインスレッドからしか触れないため)。
"""

import collections
import socket
import struct
import subprocess
import sys
import threading
import time

CAMERA_ADDRESS = "/client/current"
RIG_ADDRESS = "/client/rig"
# iPhone の REC ボタン。[clientID, 1 = 開始 / 0 = 停止]。UDP で落ちないよう iPhone が数回送ってくる
REC_ADDRESS = "/client/rec"
# 同じ指示が続けて届いたときに 1 回と数える間隔 (iPhone の再送は 0.1 秒以内)
REC_REPEAT_WINDOW = 0.5
# 端末とアプリの情報。[clientID, 機種の識別子, OS 名, OS の版, アプリの版, ビルド, 端末名]。
# 送信開始時と 5 秒おきに届く
INFO_ADDRESS = "/client/info"
# 映像 (NDI) と同じ時刻の姿勢を選ぶ・録画で全部拾うために取っておく件数。
# trkAR 1.1.1 からは毎フレーム (60Hz) 届くので 8 秒分 (1.1 までの 30Hz なら 16 秒分)
HISTORY = 480

# 機種の識別子 (utsname().machine) → 名前。確かなものだけ入れ、無いものは識別子をそのまま名前にする
DEVICE_MODELS = {
    "iPhone12,1": "iPhone 11",
    "iPhone12,3": "iPhone 11 Pro",
    "iPhone12,5": "iPhone 11 Pro Max",
    "iPhone12,8": "iPhone SE (2nd generation)",
    "iPhone13,1": "iPhone 12 mini",
    "iPhone13,2": "iPhone 12",
    "iPhone13,3": "iPhone 12 Pro",
    "iPhone13,4": "iPhone 12 Pro Max",
    "iPhone14,2": "iPhone 13 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
    "iPhone14,4": "iPhone 13 mini",
    "iPhone14,5": "iPhone 13",
    "iPhone14,6": "iPhone SE (3rd generation)",
    "iPhone14,7": "iPhone 14",
    "iPhone14,8": "iPhone 14 Plus",
    "iPhone15,2": "iPhone 14 Pro",
    "iPhone15,3": "iPhone 14 Pro Max",
    "iPhone15,4": "iPhone 15",
    "iPhone15,5": "iPhone 15 Plus",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,3": "iPhone 16",
    "iPhone17,4": "iPhone 16 Plus",
    "iPhone17,5": "iPhone 16e",
    "iPhone18,1": "iPhone 17 Pro",
    "iPhone18,2": "iPhone 17 Pro Max",
    "iPhone18,3": "iPhone 17",
    "iPhone18,4": "iPhone Air",
    "iPad11,1": "iPad mini 5", "iPad11,2": "iPad mini 5",
    "iPad14,1": "iPad mini 6", "iPad14,2": "iPad mini 6",
    "iPad16,1": "iPad mini (A17 Pro)", "iPad16,2": "iPad mini (A17 Pro)",
    "iPad12,1": "iPad (9th generation)", "iPad12,2": "iPad (9th generation)",
    "iPad13,18": "iPad (10th generation)", "iPad13,19": "iPad (10th generation)",
    "iPad15,7": "iPad (A16)", "iPad15,8": "iPad (A16)",
    "iPad13,1": "iPad Air (4th generation)", "iPad13,2": "iPad Air (4th generation)",
    "iPad13,16": "iPad Air (5th generation)", "iPad13,17": "iPad Air (5th generation)",
    "iPad14,8": "iPad Air 11-inch (M2)", "iPad14,9": "iPad Air 11-inch (M2)",
    "iPad14,10": "iPad Air 13-inch (M2)", "iPad14,11": "iPad Air 13-inch (M2)",
    "iPad15,3": "iPad Air 11-inch (M3)", "iPad15,4": "iPad Air 11-inch (M3)",
    "iPad15,5": "iPad Air 13-inch (M3)", "iPad15,6": "iPad Air 13-inch (M3)",
    "iPad13,4": "iPad Pro 11-inch (3rd generation)", "iPad13,5": "iPad Pro 11-inch (3rd generation)",
    "iPad13,6": "iPad Pro 11-inch (3rd generation)", "iPad13,7": "iPad Pro 11-inch (3rd generation)",
    "iPad13,8": "iPad Pro 12.9-inch (5th generation)", "iPad13,9": "iPad Pro 12.9-inch (5th generation)",
    "iPad13,10": "iPad Pro 12.9-inch (5th generation)", "iPad13,11": "iPad Pro 12.9-inch (5th generation)",
    "iPad14,3": "iPad Pro 11-inch (4th generation)", "iPad14,4": "iPad Pro 11-inch (4th generation)",
    "iPad14,5": "iPad Pro 12.9-inch (6th generation)", "iPad14,6": "iPad Pro 12.9-inch (6th generation)",
    "iPad16,3": "iPad Pro 11-inch (M4)", "iPad16,4": "iPad Pro 11-inch (M4)",
    "iPad16,5": "iPad Pro 13-inch (M4)", "iPad16,6": "iPad Pro 13-inch (M4)",
}


def device_model_name(identifier):
    """機種の識別子 (例 iPhone17,3) を名前 (iPhone 16) に。表に無ければ識別子のまま、空なら None。"""
    if not identifier:
        return None
    return DEVICE_MODELS.get(identifier, identifier)

_ip_lock = threading.Lock()
_ip_cache = {"at": -1e9, "list": [], "busy": False}
_KIND_ORDER = {"有線": 0, "Wi-Fi": 1}


def _kind_for(name):
    """インターフェース名 (macOS の Hardware Port / Windows の InterfaceAlias) を 有線 / Wi-Fi / その他 に分ける。"""
    low = name.lower()
    if "wi-fi" in low or "wifi" in low or "wlan" in low or "wireless" in low or "ワイヤレス" in name:
        return "Wi-Fi"
    if any(k in low for k in ("tailscale", "vpn", "utun", "zerotier", "wireguard")):
        return "VPN"
    if "vethernet" in low or "hyper-v" in low or "virtual" in low or "bridge" in low:
        return name
    if "ethernet" in low or "lan" in low or "イーサネット" in name:
        return "有線"
    return name


def _scan_macos():
    ports = {}
    out = subprocess.run(["networksetup", "-listallhardwareports"], capture_output=True, text=True, timeout=5).stdout
    port = None
    for line in out.splitlines():
        if line.startswith("Hardware Port:"):
            port = line.split(":", 1)[1].strip()
        elif line.startswith("Device:") and port:
            ports[line.split(":", 1)[1].strip()] = port
    found = []
    dev = None
    for line in subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=5).stdout.splitlines():
        if line and not line[0].isspace():
            dev = line.split(":", 1)[0]
        elif line.strip().startswith("inet ") and dev:
            ip = line.split()[1]
            # utun は Hardware Port に出てこないので名前で VPN 扱い
            name = ports.get(dev, "VPN" if dev.startswith("utun") else dev)
            found.append((ip, _kind_for(name), dev))
    return found


def _scan_windows():
    import json
    cmd = ("Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress,InterfaceAlias "
           "| ConvertTo-Json -Compress")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                         text=True, timeout=10, creationflags=flags).stdout
    rows = json.loads(out or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    return [(r["IPAddress"], _kind_for(r["InterfaceAlias"]), r["InterfaceAlias"]) for r in rows]


def _scan_fallback():
    found = []
    # UDP の connect は経路を決めるだけでパケットは送らない。既定経路の自分側アドレスが分かる
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 9))
            found.append((sock.getsockname()[0], "", ""))
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append((info[4][0], "", ""))
    except OSError:
        pass
    return found


def _refresh_ips():
    try:
        if sys.platform == "darwin":
            found = _scan_macos()
        elif sys.platform == "win32":
            found = _scan_windows()
        else:
            found = []
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        found = []
    if not found:
        found = _scan_fallback()
    seen = set()
    result = []
    for ip, kind, dev in found:
        # ループバックと、DHCP が取れていない時の自己割り当て (169.254) は送信先にならない
        if ip in seen or ip.startswith(("127.", "169.254.", "0.")):
            continue
        seen.add(ip)
        result.append((ip, kind, dev))
    result.sort(key=lambda r: _KIND_ORDER.get(r[1], 2))
    with _ip_lock:
        _ip_cache.update(at=time.monotonic(), list=result, busy=False)


def local_ipv4_addresses():
    """この PC の IPv4 アドレス一覧 [(ip, 種類, インターフェース名), ...]。有線 → Wi-Fi → その他の順。

    iPhone の送信先に入れる候補。パネルの draw から毎回呼ばれるので、調べ直しは
    5 秒おきにバックグラウンドで行い、ここでは最後の結果を返すだけにする。
    """
    if _ip_cache["at"] < 0:
        # 初回だけはその場で調べる (パネルを開いた瞬間に空欄にならないように)
        _refresh_ips()
    with _ip_lock:
        stale = time.monotonic() - _ip_cache["at"] > 5.0
        if stale and not _ip_cache["busy"]:
            _ip_cache["busy"] = True
            threading.Thread(target=_refresh_ips, name="TrkARIPScan", daemon=True).start()
        return list(_ip_cache["list"])


def _read_string(data, offset):
    end = data.index(b"\0", offset)
    text = data[offset:end].decode("utf-8", "replace")
    # OSC 文字列は NUL 込みで 4 バイト境界に揃える
    return text, (end + 4) & ~3


def parse_packet(data):
    """OSC パケットを [(address, [args...]), ...] に展開する。bundle も再帰的に開く。"""
    if data.startswith(b"#bundle\0"):
        messages = []
        offset = 16  # "#bundle\0" + timetag (8)
        while offset + 4 <= len(data):
            (size,) = struct.unpack_from(">i", data, offset)
            offset += 4
            messages.extend(parse_packet(data[offset:offset + size]))
            offset += size
        return messages

    address, offset = _read_string(data, 0)
    if offset >= len(data):
        return [(address, [])]
    tags, offset = _read_string(data, offset)
    args = []
    for tag in tags[1:]:
        if tag == "i":
            args.append(struct.unpack_from(">i", data, offset)[0])
            offset += 4
        elif tag == "f":
            args.append(struct.unpack_from(">f", data, offset)[0])
            offset += 4
        elif tag == "s":
            value, offset = _read_string(data, offset)
            args.append(value)
        elif tag == "d":
            args.append(struct.unpack_from(">d", data, offset)[0])
            offset += 8
        elif tag == "h":
            args.append(struct.unpack_from(">q", data, offset)[0])
            offset += 8
        elif tag == "T":
            args.append(True)
        elif tag == "F":
            args.append(False)
        else:
            raise ValueError("unsupported OSC type tag %r" % tag)
    return [(address, args)]


# `/client/current` の fps (iPhone の FPSType の番号) → タイムコードのフレームレート。0 (Gen) は決まっていない
TIMECODE_FPS = {7: 23.976, 8: 24.0, 1: 29.97, 2: 29.97, 3: 30.0, 4: 59.94, 5: 59.94, 6: 60.0}
# `/client/current` の tc_source → take.json に書く名前
TIMECODE_SOURCES = {0: "internal", 1: "ltc"}


class CameraPose:
    """`/client/current` 1 件分。座標は ARKit のまま (右手系 / Y 上 / メートル)。"""

    __slots__ = (
        "client_id", "app_mode", "camera_id", "position", "euler_deg",
        "timecode", "fps", "floor_height", "hfov_deg", "frame_time", "tc_seconds", "tc_source",
        "received_at",
    )

    @classmethod
    def from_args(cls, args):
        if len(args) < 9:
            return None
        pose = cls()
        pose.client_id = str(args[0])
        pose.app_mode = int(args[1])
        pose.camera_id = int(args[2])
        pose.position = (float(args[3]), float(args[4]), float(args[5]))
        pose.euler_deg = (float(args[6]), float(args[7]), float(args[8]))
        pose.timecode = tuple(int(v) for v in args[9:13]) if len(args) >= 13 else None
        pose.fps = int(args[13]) if len(args) >= 14 else 0
        pose.floor_height = float(args[14]) if len(args) >= 15 else 0.0
        # 16 番目 (index 15) は後から追加した水平 FOV。古いアプリでは来ない。
        pose.hfov_deg = float(args[15]) if len(args) >= 16 else 0.0
        # index 16 は ARFrame.timestamp (秒)。NDI 映像の timecode と同じ時計。古いアプリでは None
        pose.frame_time = float(args[16]) if len(args) >= 17 else None
        # index 17: その瞬間のタイムコード (0 時からの秒、フレーム未満まで)、
        # index 18: 0 = 内部時計 (ARKit)、1 = LTC。LTC なら外部カメラの映像と同じ時刻軸
        pose.tc_seconds = float(args[17]) if len(args) >= 18 else None
        pose.tc_source = int(args[18]) if len(args) >= 19 else None
        pose.received_at = time.monotonic()
        return pose

    @property
    def timecode_fps(self):
        return TIMECODE_FPS.get(self.fps)

    @property
    def tc_source_name(self):
        return TIMECODE_SOURCES.get(self.tc_source)


class CameraRig:
    """`/client/rig` 1 件分。iPhone のジョイスティックで積算したバーチャルカメラのオフセットとレンズ。

    座標は `/client/current` と同じ ARKit 系。受信側での合成:
        位置 = position + Ry(yaw) · 実機位置
        回転 = Ry(yaw) · 実機回転 · Rx(tilt) · Rz(roll)
    """

    __slots__ = (
        "client_id", "camera_id", "position", "yaw_deg", "tilt_deg", "roll_deg",
        "focal_mm", "fstop", "focus_distance",
    )

    @classmethod
    def from_args(cls, args):
        if len(args) < 11:
            return None
        rig = cls()
        rig.client_id = str(args[0])
        rig.camera_id = int(args[1])
        rig.position = (float(args[2]), float(args[3]), float(args[4]))
        rig.yaw_deg = float(args[5])
        rig.tilt_deg = float(args[6])
        rig.roll_deg = float(args[7])
        # 0 = AR カメラの画角に連動 / 被写界深度オフ
        rig.focal_mm = float(args[8])
        rig.fstop = float(args[9])
        rig.focus_distance = float(args[10])
        return rig


class ClientInfo:
    """`/client/info` 1 件分。古いアプリでは来ない (末尾の項目が足りなければ空文字)。"""

    __slots__ = (
        "client_id", "identifier", "system_name", "system_version", "app_version", "app_build", "device_name",
        "received_at",
    )

    @classmethod
    def from_args(cls, args):
        if len(args) < 2:
            return None
        values = [str(v) for v in args[:7]] + [""] * (7 - min(len(args), 7))
        info = cls()
        (info.client_id, info.identifier, info.system_name, info.system_version,
         info.app_version, info.app_build, info.device_name) = values
        info.received_at = time.monotonic()
        return info

    @property
    def model(self):
        return device_model_name(self.identifier)


class OSCServer:
    def __init__(self):
        self._sock = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._latest = None
        self._serial = 0
        # frame_time のある CameraPose を古い順に
        self._history = collections.deque(maxlen=HISTORY)
        self._rigs = {}
        # client ごとに最後に届いた ClientInfo (機種・OS・アプリの版)
        self._infos = {}
        # iPhone の REC の指示 (client_id, 開始か) と、client ごとに最後に受けた指示 (再送の判定用)
        self._rec_commands = collections.deque(maxlen=16)
        self._rec_last = {}
        self.packet_count = 0
        # アドレスを問わず受け取った UDP データグラム数。0 のままなら
        # そもそもネットワークで届いていない (送信先 / 権限の問題)
        self.datagram_count = 0
        self.last_error = ""
        self.port = 0
        # 最後に届いたデータグラムの送信元 IP (iPhone)
        self.last_sender = ""

    @property
    def running(self):
        return self._running

    def start(self, port, host="0.0.0.0"):
        self.stop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.settimeout(0.25)
        self._sock = sock
        self.port = port
        self.packet_count = 0
        self.datagram_count = 0
        self.last_error = ""
        self.last_sender = ""
        self._rigs = {}
        self._infos = {}
        self._history.clear()
        self._rec_commands.clear()
        self._rec_last = {}
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="TrkAROSC", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    def latest(self):
        """(serial, pose) を返す。serial が変わっていなければ新着なし。"""
        with self._lock:
            return self._serial, self._latest

    def history(self, client_id):
        """その clientID の frame_time 付きの姿勢を古い順に (コピー)。"""
        with self._lock:
            return [pose for pose in self._history if pose.client_id == client_id]

    def take_rec_commands(self):
        """届いた REC の指示 [(client_id, 開始か)] を古い順に渡して空にする (メインスレッドから呼ぶ)。"""
        with self._lock:
            commands = list(self._rec_commands)
            self._rec_commands.clear()
        return commands

    def _push_rec(self, args):
        if len(args) < 2:
            return
        client_id, start = str(args[0]), bool(int(args[1]))
        now = time.monotonic()
        with self._lock:
            # 再送で同じ指示が何度も届くので、直前と同じものは捨てる
            last = self._rec_last.get(client_id)
            self._rec_last[client_id] = (start, now)
            if last is not None and last[0] == start and now - last[1] < REC_REPEAT_WINDOW:
                return
            self._rec_commands.append((client_id, start))

    def rig_for(self, client_id):
        """その clientID から最後に届いた CameraRig (なければ None)。"""
        with self._lock:
            return self._rigs.get(client_id)

    def info_for(self, client_id):
        """その clientID から最後に届いた ClientInfo (なければ None)。"""
        with self._lock:
            return self._infos.get(client_id)

    def _loop(self):
        while self._running:
            try:
                data, sender = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._running:
                    self.last_error = str(exc)
                break
            self.datagram_count += 1
            self.last_sender = sender[0]
            try:
                self._handle(data)
            except (ValueError, struct.error) as exc:
                self.last_error = "parse: %s" % exc
            except Exception as exc:  # noqa: BLE001
                # ここで落ちるとスレッドだけ消えてソケットは開いたまま (何も届いていないように見える)
                self.last_error = "受信処理: %s: %s" % (type(exc).__name__, exc)

    def _handle(self, data):
        messages = parse_packet(data)
        for address, args in messages:
            if address == REC_ADDRESS:
                self._push_rec(args)
                continue
            if address == INFO_ADDRESS:
                info = ClientInfo.from_args(args)
                if info is not None:
                    with self._lock:
                        self._infos[info.client_id] = info
                continue
            if address == RIG_ADDRESS:
                rig = CameraRig.from_args(args)
                if rig is not None:
                    with self._lock:
                        self._rigs[rig.client_id] = rig
                continue
            if address != CAMERA_ADDRESS:
                continue
            pose = CameraPose.from_args(args)
            if pose is None:
                continue
            with self._lock:
                self._latest = pose
                self._serial += 1
                self.packet_count += 1
                if pose.frame_time is not None:
                    self._history.append(pose)
