"""NDI SDK (libndi) の送信・探索・受信 API を ctypes で包んだもの。

NDI は再配布ライセンスの都合でアドオンに同梱しない。NDI SDK / NDI Tools で
インストールされたランタイムを実行時に探して読み込む。
"""

import ctypes
import os
import sys
import time


def _fourcc(code):
    a, b, c, d = (ord(ch) for ch in code)
    return a | (b << 8) | (c << 16) | (d << 24)


FOURCC_RGBA = _fourcc("RGBA")
FOURCC_RGBX = _fourcc("RGBX")
FOURCC_BGRA = _fourcc("BGRA")
FOURCC_BGRX = _fourcc("BGRX")
FRAME_FORMAT_PROGRESSIVE = 1
TIMECODE_SYNTHESIZE = (1 << 63) - 1
FRAME_TYPE_VIDEO = 1
FRAME_TYPE_ERROR = 4
RECV_COLOR_BGRX_BGRA = 0
RECV_COLOR_RGBX_RGBA = 2
RECV_COLOR_BEST = 101  # 16bit のソース (PA16 / P216) をそのまま受ける

# Windows のこの ctypes ラッパー + NDI Runtime の組み合わせでは RGBX_RGBA 指定だと
# 受け取ったバッファが壊れる (色が緑にずれる) ことを実機で確認済み。BGRX_BGRA 指定なら
# 正しく届くので、Windows はこちらを優先するが、color_format はあくまで「希望」であって
# 保証ではない (NDI SDK の仕様) ので、実際に来たフレームがどちらだったかは毎回
# frame.FourCC を見て to_rgba_order() で判断する (macOS は元から RGBX_RGBA で問題ない)
DEFAULT_RECV_COLOR_FORMAT = RECV_COLOR_BGRX_BGRA if sys.platform == "win32" else RECV_COLOR_RGBX_RGBA


def to_rgba_order(pixels, fourcc):
    """Receiver が返す (..., 4) の並びを、実際に届いた FourCC を見て RGBA に揃える。"""
    return pixels[..., (2, 1, 0, 3)] if fourcc in (FOURCC_BGRA, FOURCC_BGRX) else pixels
FOURCC_PA16 = _fourcc("PA16")
FOURCC_P216 = _fourcc("P216")
RECV_BANDWIDTH_LOWEST = 0
RECV_BANDWIDTH_HIGHEST = 100


class Rate:
    """数えたフレームから実際の fps を出す (1 秒ごとに更新)。tick は送受信のスレッドから呼んでよい。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self._fps = 0.0
        self._count = 0
        self._since = time.perf_counter()

    def tick(self):
        self._count += 1
        now = time.perf_counter()
        if now - self._since >= 1.0:
            self._fps = self._count / (now - self._since)
            self._count = 0
            self._since = now

    @property
    def fps(self):
        # 止まったときに古い値を出し続けないよう、2 秒来なければ来た分だけで割る
        elapsed = time.perf_counter() - self._since
        return self._count / elapsed if elapsed > 2.0 else self._fps


class SendCreate(ctypes.Structure):
    _fields_ = [
        ("p_ndi_name", ctypes.c_char_p),
        ("p_groups", ctypes.c_char_p),
        ("clock_video", ctypes.c_bool),
        ("clock_audio", ctypes.c_bool),
    ]


class VideoFrameV2(ctypes.Structure):
    _fields_ = [
        ("xres", ctypes.c_int),
        ("yres", ctypes.c_int),
        ("FourCC", ctypes.c_int),
        ("frame_rate_N", ctypes.c_int),
        ("frame_rate_D", ctypes.c_int),
        ("picture_aspect_ratio", ctypes.c_float),
        ("frame_format_type", ctypes.c_int),
        ("timecode", ctypes.c_int64),
        ("p_data", ctypes.c_void_p),
        ("line_stride_in_bytes", ctypes.c_int),
        ("p_metadata", ctypes.c_char_p),
        ("timestamp", ctypes.c_int64),
    ]


class FindCreate(ctypes.Structure):
    _fields_ = [
        ("show_local_sources", ctypes.c_bool),
        ("p_groups", ctypes.c_char_p),
        ("p_extra_ips", ctypes.c_char_p),
    ]


class Source(ctypes.Structure):
    _fields_ = [
        ("p_ndi_name", ctypes.c_char_p),
        ("p_url_address", ctypes.c_char_p),
    ]


class RecvCreateV3(ctypes.Structure):
    _fields_ = [
        ("source_to_connect_to", Source),
        ("color_format", ctypes.c_int),
        ("bandwidth", ctypes.c_int),
        ("allow_video_fields", ctypes.c_bool),
        ("p_ndi_recv_name", ctypes.c_char_p),
    ]


def candidate_paths(override=""):
    paths = []
    if override:
        paths.append(override)
    runtime_dir = os.environ.get("NDI_RUNTIME_DIR_V6") or os.environ.get("NDI_RUNTIME_DIR_V5")
    if sys.platform == "darwin":
        name = "libndi.dylib"
        if runtime_dir:
            paths.append(os.path.join(runtime_dir, name))
        paths += [
            "/Library/NDI SDK for Apple/lib/macOS/libndi.dylib",
            "/usr/local/lib/libndi.dylib",
            "/opt/homebrew/lib/libndi.dylib",
            "/Library/Application Support/NDI/lib/libndi.dylib",
        ]
    elif sys.platform == "win32":
        name = "Processing.NDI.Lib.x64.dll"
        if runtime_dir:
            paths.append(os.path.join(runtime_dir, name))
        paths.append(name)
    else:
        name = "libndi.so.6"
        if runtime_dir:
            paths.append(os.path.join(runtime_dir, name))
        paths += [name, "libndi.so"]
    return paths


_lib = None
_lib_path = ""


def load(override=""):
    """libndi を読み込んで初期化する。失敗したら RuntimeError。"""
    global _lib, _lib_path
    if _lib is not None:
        return _lib

    errors = []
    for path in candidate_paths(override):
        if os.path.isabs(path) and not os.path.exists(path):
            continue
        try:
            lib = ctypes.CDLL(path)
        except OSError as exc:
            errors.append("%s: %s" % (path, exc))
            continue
        _lib_path = path
        break
    else:
        raise RuntimeError(
            "NDI ランタイム (libndi) が見つかりません。NDI SDK か NDI Tools を"
            "インストールするか、アドオン設定でパスを指定してください。\n" + "\n".join(errors)
        )

    lib.NDIlib_initialize.restype = ctypes.c_bool
    lib.NDIlib_send_create.argtypes = [ctypes.POINTER(SendCreate)]
    lib.NDIlib_send_create.restype = ctypes.c_void_p
    lib.NDIlib_send_destroy.argtypes = [ctypes.c_void_p]
    lib.NDIlib_send_destroy.restype = None
    lib.NDIlib_send_send_video_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(VideoFrameV2)]
    lib.NDIlib_send_send_video_v2.restype = None
    lib.NDIlib_send_get_no_connections.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.NDIlib_send_get_no_connections.restype = ctypes.c_int

    lib.NDIlib_find_create_v2.argtypes = [ctypes.POINTER(FindCreate)]
    lib.NDIlib_find_create_v2.restype = ctypes.c_void_p
    lib.NDIlib_find_destroy.argtypes = [ctypes.c_void_p]
    lib.NDIlib_find_destroy.restype = None
    lib.NDIlib_find_get_current_sources.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    lib.NDIlib_find_get_current_sources.restype = ctypes.POINTER(Source)
    lib.NDIlib_recv_create_v3.argtypes = [ctypes.POINTER(RecvCreateV3)]
    lib.NDIlib_recv_create_v3.restype = ctypes.c_void_p
    lib.NDIlib_recv_destroy.argtypes = [ctypes.c_void_p]
    lib.NDIlib_recv_destroy.restype = None
    lib.NDIlib_recv_capture_v3.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(VideoFrameV2), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
    ]
    lib.NDIlib_recv_capture_v3.restype = ctypes.c_int
    lib.NDIlib_recv_free_video_v2.argtypes = [ctypes.c_void_p, ctypes.POINTER(VideoFrameV2)]
    lib.NDIlib_recv_free_video_v2.restype = None

    if not lib.NDIlib_initialize():
        raise RuntimeError("NDIlib_initialize() に失敗しました (CPU 非対応?)")
    _lib = lib
    return lib


def loaded_path():
    return _lib_path


class Sender:
    def __init__(self, name, library_override=""):
        self._lib = load(library_override)
        self._name = name.encode("utf-8")  # send_create 中は生存させておく
        settings = SendCreate(self._name, None, False, False)
        self._instance = self._lib.NDIlib_send_create(ctypes.byref(settings))
        if not self._instance:
            raise RuntimeError("NDIlib_send_create() に失敗しました")
        self._frame = VideoFrameV2()

    def send_rgba(self, pixels, width, height, fps, with_alpha, timecode=TIMECODE_SYNTHESIZE):
        """pixels: 上から下へ並んだ連続 RGBA8 バッファ (numpy 配列など)。同期送信。
        timecode は 100ns 単位 (受けた映像の timecode を引き継ぐときに渡す)。"""
        if not self._instance:
            return
        frame = self._frame
        frame.xres = width
        frame.yres = height
        frame.FourCC = FOURCC_RGBA if with_alpha else FOURCC_RGBX
        frame.frame_rate_N = int(round(fps * 1000))
        frame.frame_rate_D = 1000
        frame.picture_aspect_ratio = 0.0
        frame.frame_format_type = FRAME_FORMAT_PROGRESSIVE
        frame.timecode = timecode
        frame.p_data = pixels.ctypes.data
        frame.line_stride_in_bytes = width * 4
        frame.p_metadata = None
        self._lib.NDIlib_send_send_video_v2(self._instance, ctypes.byref(frame))

    def connections(self):
        if not self._instance:
            return 0
        return self._lib.NDIlib_send_get_no_connections(self._instance, 0)

    def close(self):
        if self._instance:
            self._lib.NDIlib_send_destroy(self._instance)
            self._instance = None


class Finder:
    """ネットワーク上の NDI ソースを探し続ける。"""

    def __init__(self, library_override=""):
        self._lib = load(library_override)
        settings = FindCreate(True, None, None)
        self._instance = self._lib.NDIlib_find_create_v2(ctypes.byref(settings))
        if not self._instance:
            raise RuntimeError("NDIlib_find_create_v2() に失敗しました")

    def sources(self):
        """見つかっているソース名の一覧 (例: "IPHONE (trkAR)")。"""
        if not self._instance:
            return []
        count = ctypes.c_uint32(0)
        array = self._lib.NDIlib_find_get_current_sources(self._instance, ctypes.byref(count))
        names = []
        for i in range(count.value):
            name = array[i].p_ndi_name
            if name:
                names.append(name.decode("utf-8", "replace"))
        return names

    def close(self):
        if self._instance:
            self._lib.NDIlib_find_destroy(self._instance)
            self._instance = None


class Receiver:
    """1 つのソースから映像を受ける (既定は RGBA8。Windows は内部的に BGRA8 で受けて
    to_rgba_order() で揃える)。capture は受信スレッドから呼ぶ。"""

    def __init__(self, source_name, low_bandwidth=False, library_override="", color_format=None):
        if color_format is None:
            color_format = DEFAULT_RECV_COLOR_FORMAT
        self._lib = load(library_override)
        self._source_name = source_name.encode("utf-8")
        self._recv_name = b"trkAR Camera (Blender)"
        settings = RecvCreateV3(
            Source(self._source_name, None),
            color_format,
            RECV_BANDWIDTH_LOWEST if low_bandwidth else RECV_BANDWIDTH_HIGHEST,
            False,
            self._recv_name,
        )
        self._instance = self._lib.NDIlib_recv_create_v3(ctypes.byref(settings))
        if not self._instance:
            raise RuntimeError("NDIlib_recv_create_v3() に失敗しました")
        self._frame = VideoFrameV2()

    def capture(self, copy_frame, timeout_ms=100):
        """映像が届いたら copy_frame(VideoFrameV2) を呼んでから解放する。

        戻り値: 映像を受けたら True、タイムアウトなら False。接続が切れたら RuntimeError。
        """
        if not self._instance:
            return False
        frame = self._frame
        kind = self._lib.NDIlib_recv_capture_v3(self._instance, ctypes.byref(frame), None, None, timeout_ms)
        if kind == FRAME_TYPE_ERROR:
            raise RuntimeError("NDI の接続が切れました")
        if kind != FRAME_TYPE_VIDEO:
            return False
        try:
            copy_frame(frame)
        finally:
            self._lib.NDIlib_recv_free_video_v2(self._instance, ctypes.byref(frame))
        return True

    def close(self):
        if self._instance:
            self._lib.NDIlib_recv_destroy(self._instance)
            self._instance = None
