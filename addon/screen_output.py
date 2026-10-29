"""Blender の画面 (ウィンドウ全体か 1 つのエリア) をそのまま NDI で送り出す。

OBS で画面を取り込んで NDI にしていたのを Blender 単体でやる。画面の読み取りは
`bpy.types.Window.screenshot()` (Blender 5.3 から) を使う。メニューやポップアップも含めて
ウィンドウに描かれたものがそのまま取れ、1 枚 数 ms で済む。macOS の画面収録の
許可は要らない (OS ではなく Blender が自分の描画を読むため)。マウスカーソルは OS が
描くので映らない。

読み取りはメインスレッドのタイマーで行い、上下反転・切り出し・縮小と送信は送信
スレッドでやる (メインスレッドを止めない)。
"""

import queue
import threading
import time

import bpy
import numpy as np

from . import ndi


def supported():
    return hasattr(bpy.types.Window, "screenshot")


class ScreenOutput:
    def __init__(self):
        self.sender = None
        self.window_pointer = 0
        self.area_pointer = 0  # 0 ならウィンドウ全体
        self.fps = 30.0
        self.half = False
        self.width = 0
        self.height = 0
        self.frames_sent = 0
        # 実際の fps (読み取り / 送出)
        self.capture_rate = ndi.Rate()
        self.send_rate = ndi.Rate()
        self.capture_ms = 0.0
        self.send_ms = 0.0
        self.last_error = ""
        self._queue = queue.Queue(maxsize=1)
        self._thread = None
        self._next = 0.0
        # タイマーに渡すのはいつも同じ関数にする (bpy.app.timers は関数で見分ける)
        self._timer = self._tick

    @property
    def running(self):
        return self.sender is not None

    def start(self, window, area, name, fps, half, library_override=""):
        """area が None ならウィンドウ全体、あればそのエリアだけを送る。"""
        if not supported():
            raise RuntimeError("画面の送出には Blender 5.3 以降が必要です (Window.screenshot がありません)")
        self.stop()
        self.sender = ndi.Sender(name, library_override)
        self.window_pointer = window.as_pointer()
        self.area_pointer = area.as_pointer() if area is not None else 0
        self.fps = fps
        self.half = half
        self.width = self.height = 0
        self.frames_sent = 0
        self.capture_rate.reset()
        self.send_rate.reset()
        self.capture_ms = self.send_ms = 0.0
        self.last_error = ""
        self._next = 0.0
        self._thread = threading.Thread(target=self._send_loop, name="TrkARScreenNDI", daemon=True)
        self._thread.start()
        # 止めたタイマーを同じ周回で登録し直すと動かないことがあるので、タイマーは外さず
        # 送出が止まっていたら _tick が自分で抜ける (止めてすぐ開始したときはそのまま続く)
        if not bpy.app.timers.is_registered(self._timer):
            bpy.app.timers.register(self._timer, persistent=True)

    def stop(self):
        if self._thread is not None:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(None)
            self._thread.join(timeout=1.0)
            self._thread = None
        if self.sender is not None:
            self.sender.close()
            self.sender = None
        self.window_pointer = 0
        self.area_pointer = 0

    def _find(self):
        """(window, area)。ウィンドウが閉じられていたら最初のウィンドウに移る。"""
        windows = bpy.context.window_manager.windows
        window = next((w for w in windows if w.as_pointer() == self.window_pointer), None)
        if window is None:
            if self.area_pointer or not windows:
                return None, None
            window = windows[0]
            self.window_pointer = window.as_pointer()
        if not self.area_pointer:
            return window, None
        area = next((a for a in window.screen.areas if a.as_pointer() == self.area_pointer), None)
        return window, area

    def _tick(self):
        if self.sender is None:
            return None
        interval = 1.0 / max(1.0, self.fps)
        now = time.perf_counter()
        # 送るのが遅れても詰めて撮らず、次の枠から数え直す
        self._next = max(self._next + interval, now)
        try:
            self._capture()
        except Exception as exc:  # タイマーで例外を上げると止まってしまう
            self.last_error = str(exc)
        return max(0.001, self._next - time.perf_counter())

    def _capture(self):
        window, area = self._find()
        if window is None:
            self.last_error = "送るウィンドウ / エリアが見つかりません (閉じたか、画面を切り替えた)"
            return
        t0 = time.perf_counter()
        shot = window.screenshot()
        self._average("capture_ms", time.perf_counter() - t0)
        self.capture_rate.tick()
        crop = None
        if area is not None:
            # Retina などでは画素がウィンドウの座標より細かいので比で合わせる
            scale = shot.shape[1] / max(1, window.width)
            crop = (int(area.x * scale), int(area.y * scale),
                    int((area.x + area.width) * scale), int((area.y + area.height) * scale))
        self.last_error = ""
        try:
            self._queue.put_nowait((shot, crop))
        except queue.Full:
            # 送信が詰まっているときは古いフレームを捨てて最新を優先
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait((shot, crop))

    def _average(self, attr, seconds):
        value = getattr(self, attr)
        setattr(self, attr, value + (seconds * 1000.0 - value) * 0.1)

    def _send_loop(self):
        while True:
            frame = self._queue.get()
            if frame is None:
                return
            sender = self.sender
            if sender is None:
                return
            try:
                t0 = time.perf_counter()
                shot, crop = frame
                pixels = np.asarray(shot)  # (高さ, 幅, 4) RGBA8、下の行から
                if crop is not None:
                    x0, y0, x1, y1 = crop
                    pixels = pixels[max(0, y0):y1, max(0, x0):x1]
                if self.half:
                    pixels = pixels[::2, ::2]
                # NDI は奇数の幅・高さだと端の 1 画素を落とすので、先に偶数にそろえておく
                height, width = pixels.shape[0] & ~1, pixels.shape[1] & ~1
                if width == 0 or height == 0:
                    continue
                # Blender は左下原点、NDI は左上原点
                pixels = np.ascontiguousarray(pixels[:height, :width][::-1])
                sender.send_rgba(pixels, width, height, self.fps, False)
                self.width, self.height = width, height
                self.frames_sent += 1
                self.send_rate.tick()
                self._average("send_ms", time.perf_counter() - t0)
            except Exception as exc:
                self.last_error = str(exc)
