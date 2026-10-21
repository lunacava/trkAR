"""追従カメラ視点をオフスクリーン描画して NDI で送り出す。

GPU 描画は 3D ビューの描画コールバック内でしか行えないので、送出開始した
3D ビュー (エリア) の draw handler で `GPUOffScreen.draw_view3d` を呼び、
読み戻したピクセルを送信スレッドへ渡す。シェーディング (ソリッド /
マテリアル / レンダー) やオーバーレイ表示はその 3D ビューの設定に従う。
"""

import queue
import threading
import time

import bpy
import gpu
import numpy as np

from . import ndi


def _as_rgba8(buffer, width, height):
    """gpu.types.Buffer (RGBA8) を (height, width, 4) の uint8 配列として見る (コピーなし)。

    Blender 5.1 の Buffer はバッファプロトコルで逆順のストライド
    (1, H, H*W) を公開するが、実メモリは C 順に詰まっている。numpy が
    そのストライドに従うと画像が崩れるので、C 順のストライドを付け直す。
    """
    raw = np.asarray(buffer)
    if raw.flags.c_contiguous:
        pixels = raw.reshape(height, width, 4)
    else:
        item = raw.itemsize
        pixels = np.lib.stride_tricks.as_strided(
            raw, shape=(height, width, 4), strides=(width * 4 * item, 4 * item, item)
        )
    if pixels.dtype != np.uint8:
        pixels = (np.clip(pixels, 0.0, 1.0) * 255.0 + 0.5).astype(np.uint8)
    return pixels


class NDIOutput:
    # 読み戻しを 1 フレーム遅らせる (オフスクリーンを 2 枚交互に使う)。描いた直後に読むと
    # GPU の描画が終わるまで Blender の描画が止まるので、前のフレームで描いた方を読む
    PIPELINE = True

    def __init__(self):
        self.sender = None
        self.offscreen = None
        self._offscreens = []
        self._index = 0
        self._pending = False
        # 直近の所要時間 (ms、指数平均): 描画 / 読み戻し
        self.draw_ms = 0.0
        self.read_ms = 0.0
        self.area_pointer = 0
        self.width = 0
        self.height = 0
        self.fps = 30.0
        self.transparent = False
        self.frames_sent = 0
        self.last_error = ""
        self._handle = None
        self._last_draw = 0.0
        self._queue = queue.Queue(maxsize=1)
        self._thread = None
        self._drawing = False

    @property
    def running(self):
        return self.sender is not None

    def start(self, area, name, width, height, fps, transparent, library_override=""):
        self.stop()
        self.sender = ndi.Sender(name, library_override)
        self._offscreens = [gpu.types.GPUOffScreen(width, height) for _ in range(2 if self.PIPELINE else 1)]
        self._index = 0
        self._pending = False
        self.offscreen = self._offscreens[0]
        self.draw_ms = self.read_ms = 0.0
        self.area_pointer = area.as_pointer()
        self.width = width
        self.height = height
        self.fps = fps
        self.transparent = transparent
        self.frames_sent = 0
        self.last_error = ""
        self._last_draw = 0.0
        self._thread = threading.Thread(target=self._send_loop, name="ARtrkNDI", daemon=True)
        self._thread.start()
        self._handle = bpy.types.SpaceView3D.draw_handler_add(self._draw, (), "WINDOW", "POST_PIXEL")
        bpy.app.timers.register(self._tick, persistent=True)

    def stop(self):
        if self._handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self._handle, "WINDOW")
            self._handle = None
        if bpy.app.timers.is_registered(self._tick):
            bpy.app.timers.unregister(self._tick)
        if self._thread is not None:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(None)
            self._thread.join(timeout=1.0)
            self._thread = None
        for offscreen in self._offscreens:
            offscreen.free()
        self._offscreens = []
        self.offscreen = None
        self._pending = False
        if self.sender is not None:
            self.sender.close()
            self.sender = None
        self.area_pointer = 0

    def _find_area(self):
        wm = bpy.context.window_manager
        for window in wm.windows:
            for area in window.screen.areas:
                if area.as_pointer() == self.area_pointer:
                    return area
        return None

    def _tick(self):
        # カメラが静止していても一定レートで描画させるため再描画を要求する
        if self.sender is None:
            return None
        area = self._find_area()
        if area is not None:
            area.tag_redraw()
        return 1.0 / max(1.0, self.fps)

    def _draw(self):
        if self._drawing or self.sender is None:
            return
        context = bpy.context
        if context.area is None or context.area.as_pointer() != self.area_pointer:
            return
        now = time.perf_counter()
        # tick と描画のズレでフレームを落としすぎないよう少しだけ早めに許容する
        if now - self._last_draw < 0.9 / self.fps:
            return
        scene = context.scene
        camera = scene.artrk.camera or scene.camera
        if camera is None or camera.type != "CAMERA":
            self.last_error = "送出するカメラがありません"
            return
        self._last_draw = now
        self._drawing = True
        try:
            self._render(context, scene, camera)
        except Exception as exc:  # 描画コールバックで例外を上げると毎フレーム出続ける
            self.last_error = str(exc)
        finally:
            self._drawing = False

    def _render(self, context, scene, camera):
        depsgraph = context.evaluated_depsgraph_get()
        render = scene.render
        view_matrix = camera.matrix_world.inverted()
        projection_matrix = camera.calc_matrix_camera(
            depsgraph,
            x=self.width,
            y=self.height,
            scale_x=render.pixel_aspect_x,
            scale_y=render.pixel_aspect_y,
        )
        pipelined = len(self._offscreens) > 1
        t0 = time.perf_counter()
        frame = None
        if pipelined and self._pending:
            # 前のフレームで描いた方を、新しく描く前に読む。読み戻しは GPU の仕事が
            # 全部終わるのを待つので、描いた後に読むと今描いた分まで待つことになる
            frame = self._read(self._offscreens[1 - self._index])
        t1 = time.perf_counter()
        target = self._offscreens[self._index]
        target.draw_view3d(
            scene,
            context.view_layer,
            context.space_data,
            context.region,
            view_matrix,
            projection_matrix,
            do_color_management=True,
            draw_background=not self.transparent,
        )
        t2 = time.perf_counter()
        if pipelined:
            self._pending = True
            self._index = 1 - self._index
        else:
            frame = self._read(target)
        t3 = time.perf_counter()
        self._timing(t2 - t1, (t1 - t0) + (t3 - t2))
        if frame is None:
            return
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            # 送信が詰まっているときは古いフレームを捨てて最新を優先
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._queue.put_nowait(frame)

    def _read(self, offscreen):
        buffer = offscreen.texture_color.read()
        # 上下反転のコピーは送信スレッドでやる (描画を止めない)。buffer ごと渡して生かしておく
        return buffer, _as_rgba8(buffer, self.width, self.height)

    def _timing(self, draw, read):
        a = 0.1
        self.draw_ms += (draw * 1000.0 - self.draw_ms) * a
        self.read_ms += (read * 1000.0 - self.read_ms) * a

    def _send_loop(self):
        while True:
            frame = self._queue.get()
            if frame is None:
                return
            sender = self.sender
            if sender is None:
                return
            try:
                # GPU は左下原点、NDI は左上原点
                _buffer, view = frame
                pixels = np.ascontiguousarray(view[::-1])
                sender.send_rgba(pixels, self.width, self.height, self.fps, self.transparent)
                self.frames_sent += 1
            except Exception as exc:
                self.last_error = str(exc)
