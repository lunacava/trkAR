"""Blender の画面 (ウィンドウ全体か 1 つのエリア) を、OS の画面キャプチャ (macOS の ScreenCaptureKit) で取って NDI で送る。

Blender の中で読む screen_output (Window.screenshot) は、GPU がそのとき描いているコマを描き終わるのを
メインスレッドで待つので、重いシーンではシーンの fps が落ちる (classroom のマテリアルプレビューで 22 → 16)。
こちらは別のプロセス (bin/trkar-screen-ndi、ソースは tools/screen_ndi) が OS の合成したウィンドウの絵を GPU のまま
受け取って送るので、Blender は待たされない。代わりに OS の合成を通る分だけ遅れが少し増える。

macOS 14 以降。Mac の「画面収録」の許可が要る (Blender に出す。初めてのときは OS が尋ねる)。
"""

import os
import subprocess
import sys
import threading

import bpy

HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "trkar-screen-ndi")
# 送る範囲 (エリア) が変わっていないかを見る間隔 (秒)
WATCH_INTERVAL = 0.5
PERMISSION_HELP = ("画面収録の許可が要ります: システム設定 > プライバシーとセキュリティ > 画面とシステムオーディオ録音 "
                   "で Blender をオンにして、Blender を起動し直す")


def supported():
    if sys.platform != "darwin" or not os.path.isfile(HELPER):
        return False
    try:
        major = int(subprocess.run(["sw_vers", "-productVersion"], capture_output=True, text=True,
                                   timeout=2).stdout.split(".")[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return major >= 14


class OSScreenOutput:
    def __init__(self):
        self._process = None
        self._reader = None
        self.window_pointer = 0
        self.area_pointer = 0
        self.fps = 0
        self.width = 0
        self.height = 0
        self.connections = 0
        self.last_error = ""
        self._geometry = None
        self._timer = self._watch

    @property
    def running(self):
        return self._process is not None and self._process.poll() is None

    def start(self, window, area, name, fps, half, cursor):
        """area が None ならウィンドウ全体、あればそのエリアだけを送る。"""
        self.stop()
        if not supported():
            raise RuntimeError("OS の画面キャプチャは macOS 14 以降で使えます (%s が要ります)" % HELPER)
        if not os.access(HELPER, os.X_OK):
            os.chmod(HELPER, 0o755)  # zip から入れると実行の印が落ちることがある
        self.window_pointer = window.as_pointer()
        self.area_pointer = area.as_pointer() if area is not None else 0
        self.fps = self.width = self.height = self.connections = 0
        self.last_error = ""
        self._geometry = self._measure(window, area)
        args = [HELPER, "--pid", str(os.getpid()), "--parent", str(os.getpid()), "--name", name,
                "--fps", str(int(round(fps))), "--aspect", "%.6f" % self._geometry[-1]]
        if area is not None:
            args += ["--rect", ",".join("%.6f" % v for v in self._geometry[:4])]
        if half:
            args.append("--half")
        if not cursor:
            args.append("--no-cursor")
        self._process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, bufsize=1)
        self._reader = threading.Thread(target=self._read, args=(self._process,), name="TrkAROSScreen", daemon=True)
        self._reader.start()
        if not bpy.app.timers.is_registered(self._timer):
            bpy.app.timers.register(self._timer, first_interval=WATCH_INTERVAL, persistent=True)

    def stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def _read(self, process):
        """送出アプリの標準出力 (STATUS / ERROR) を読む。"""
        for line in process.stdout:
            line = line.strip()
            if line.startswith("STATUS"):
                fields = dict(part.split("=", 1) for part in line.split()[1:] if "=" in part)
                try:
                    self.fps = int(fields.get("fps", 0))
                    self.width, self.height = (int(v) for v in fields.get("size", "0x0").split("x"))
                    self.connections = int(fields.get("conns", 0))
                except ValueError:
                    pass
            elif line.startswith("ERROR"):
                message = line[len("ERROR "):]
                self.last_error = PERMISSION_HELP if "permission" in message else message
        code = process.wait()
        if code not in (0, -15) and not self.last_error:
            self.last_error = "送出アプリが止まりました (終了コード %d)" % code

    @staticmethod
    def _measure(window, area):
        """(送る範囲の割合 x, y, 幅, 高さ (左下が原点), 中身の幅 / 高さ)。"""
        aspect = window.width / max(1, window.height)
        if area is None:
            return 0.0, 0.0, 1.0, 1.0, aspect
        return (area.x / window.width, area.y / window.height, area.width / window.width,
                area.height / window.height, aspect)

    def _find(self):
        for window in bpy.context.window_manager.windows:
            if window.as_pointer() == self.window_pointer:
                if not self.area_pointer:
                    return window, None
                area = next((a for a in window.screen.areas if a.as_pointer() == self.area_pointer), None)
                return window, area
        return None, None

    def _watch(self):
        """エリアやウィンドウの大きさが変わったら、送る範囲を送出アプリに伝える (止めずに変える)。"""
        if not self.running:
            return None
        window, area = self._find()
        if window is not None and (area is not None or not self.area_pointer):
            geometry = self._measure(window, area)
            if geometry != self._geometry:
                self._geometry = geometry
                line = ("rect %.6f %.6f %.6f %.6f %.6f\n" % geometry) if area is not None \
                    else ("full %.6f\n" % geometry[-1])
                try:
                    self._process.stdin.write(line)
                    self._process.stdin.flush()
                except (OSError, ValueError):
                    pass
        return WATCH_INTERVAL
