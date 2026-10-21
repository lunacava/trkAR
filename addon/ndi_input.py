"""NDI の映像を受けて Blender の画像 (bpy.types.Image) に流し込む。

受信と float 変換は受信スレッドで行い、メインスレッドのタイマーでは最新の
1 枚を `image.pixels.foreach_set` で書き込むだけにする。画像は普通の画像
データブロックなので、マテリアルの画像テクスチャ・カメラ背景・ワールドなど
どこからでも参照できる。
"""

import ctypes
import threading

import bpy
import numpy as np

from . import ndi

IMAGE_NAME = "ARtrk NDI"


def ensure_image(width=16, height=9):
    image = bpy.data.images.get(IMAGE_NAME)
    if image is None:
        image = bpy.data.images.new(IMAGE_NAME, width, height, alpha=True)
        image.generated_color = (0.0, 0.0, 0.0, 1.0)
    return image


def blank_live_images(names):
    """ライブ映像の画像を黒の生成画像に戻す (.blend の保存の前に呼ぶ)。

    中身を書き換えた生成画像は、保存のときに Blender が .blend に埋め込む。
    そのままだと保存した瞬間のカメラのコマ (人や部屋) がシーンに残るので、空にしてから保存させる。
    次のフレームが来れば今までどおり映る。"""
    for name in names:
        image = bpy.data.images.get(name)
        if image is None:
            continue
        if image.packed_file is not None:
            image.unpack(method="REMOVE")
        image.source = "GENERATED"
        image.generated_type = "BLANK"
        image.generated_color = (0.0, 0.0, 0.0, 1.0)
        image.generated_width, image.generated_height = 16, 9


class NDIInput:
    def __init__(self):
        self.receiver = None
        self.source_name = ""
        self.frames_received = 0
        self.frames_shown = 0
        self.resolution = (0, 0)
        self.last_error = ""
        self._finder = None
        self._thread = None
        self._running = False
        self._lock = threading.Lock()
        self._pending = None
        # 表示中のフレームの timecode (100ns)。深度スクリーンが同じ時刻の深度を選ぶのに使う
        self.shown_timecode = None
        # 受けている映像にアルファがあるか (artrk_matte.py の "ARtrk Matte" など)
        self.has_alpha = False
        # 録画中 (take.Recorder) は表示中のフレームを 8bit のまま (左上原点) 持っておく
        self.keep_rgba = False
        self.shown_rgba = None
        # depth_input が ARKit の人物マスクを直接アルファとして使うためのフック。
        # callable(width, height) -> (height, width) の 0..1 float32 (Blender と同じ左下原点) か None
        self.alpha_override = None

    @property
    def running(self):
        return self._running

    # --- ソース探索 (メニューから呼ぶ) ---

    def sources(self, library_override=""):
        if self._finder is None:
            self._finder = ndi.Finder(library_override)
        return self._finder.sources()

    # --- 受信 ---

    def start(self, source_name, low_bandwidth=False, library_override=""):
        self.stop()
        self.receiver = ndi.Receiver(source_name, low_bandwidth, library_override)
        self.source_name = source_name
        self.frames_received = 0
        self.frames_shown = 0
        self.resolution = (0, 0)
        self.last_error = ""
        self._pending = None
        self.shown_timecode = None
        # 受けている映像にアルファがあるか (artrk_matte.py の "ARtrk Matte" など)
        self.has_alpha = False
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, name="ARtrkNDIIn", daemon=True)
        self._thread.start()
        ensure_image()
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
        self._pending = None

    def close(self):
        self.stop()
        if self._finder is not None:
            self._finder.close()
            self._finder = None

    def _copy(self, frame):
        width, height, stride = frame.xres, frame.yres, frame.line_stride_in_bytes
        size = stride * height
        raw = np.ctypeslib.as_array(ctypes.cast(frame.p_data, ctypes.POINTER(ctypes.c_uint8)), shape=(size,))
        pixels = raw.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
        pixels = ndi.to_rgba_order(pixels, frame.FourCC)
        # NDI は左上原点、Blender の画像は左下原点。ここで反転と 0..1 化まで済ませておく
        floats = np.empty((height, width, 4), dtype=np.float32)
        np.multiply(pixels[::-1], 1.0 / 255.0, out=floats, casting="unsafe")
        rgba = pixels.copy() if self.keep_rgba else None
        with self._lock:
            self._pending = (floats, frame.timecode, rgba)
        self.has_alpha = frame.FourCC in (ndi.FOURCC_RGBA, ndi.FOURCC_BGRA)
        self.frames_received += 1

    def _recv_loop(self):
        while self._running:
            receiver = self.receiver
            if receiver is None:
                return
            try:
                receiver.capture(self._copy, 100)
            except Exception as exc:
                self.last_error = str(exc)

    def _tick(self):
        if not self._running:
            return None
        with self._lock:
            pending, self._pending = self._pending, None
        if pending is not None:
            floats, timecode, rgba = pending
            try:
                self._upload(floats)
                self.shown_timecode = timecode
                self.shown_rgba = rgba
            except Exception as exc:  # 画像が消された / 編集中など。次のフレームで作り直す
                self.last_error = str(exc)
        return 1.0 / 60.0

    def show_rgba(self, rgba):
        """録画したフレーム ((h, w, 4) uint8、左上原点) を受信したときと同じ画像に出す (テイク再生)。"""
        floats = np.empty(rgba.shape, dtype=np.float32)
        np.multiply(rgba[::-1], 1.0 / 255.0, out=floats, casting="unsafe")
        self._upload(floats)

    def _upload(self, floats):
        height, width = floats.shape[:2]
        if self.alpha_override is not None:
            alpha = self.alpha_override(width, height)
            if alpha is not None:
                floats[..., 3] = alpha
                self.has_alpha = True
        image = ensure_image(width, height)
        if tuple(image.size) != (width, height):
            image.scale(width, height)
        image.pixels.foreach_set(floats.ravel())
        image.update()
        self.resolution = (width, height)
        self.frames_shown += 1
        # マテリアル / カメラ背景を表示しているビューを描き直す
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type in {"VIEW_3D", "IMAGE_EDITOR", "NODE_EDITOR"}:
                    area.tag_redraw()
