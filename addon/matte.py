"""RVM の切り抜き中継 (tools/matte/trkar_matte.py) を Blender から起動・停止する。

中継は別プロセス (Core ML / ONNX Runtime と NDI を Blender の外で回す)。Blender の NDI 受信は
中継の出す "<PC名> (trkAR Matte)" に自動で切り替え、止めたら元のソースに戻す。
"""

import os
import subprocess
import sys
import threading
import time
from collections import deque
try:
    from .i18n import _t
except ImportError:  # アドオンの外から単独で読まれたとき (render_clay.py・GTN のノードなど)。訳さない
    def _t(message):
        return message

SCRIPT = "trkar_matte.py"
OUTPUT_NAME = "trkAR Matte"


def find_folder(configured=""):
    """trkar_matte.py のあるフォルダ。設定が空ならアドオンの近く (リポジトリの tools/matte) を探す。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [configured] if configured else [
        os.path.join(here, "..", "..", "tools", "matte"),  # 開発のリポジトリ (blender/trkar_camera の 2 つ上)
        os.path.join(here, "..", "tools", "matte"),        # 公開のリポジトリ (addon/ の隣)
    ]
    for folder in candidates:
        if folder and os.path.isfile(os.path.join(folder, SCRIPT)):
            return os.path.normpath(folder)
    return None


def python_for(folder):
    for path in (os.path.join(folder, ".venv", "bin", "python"),
                 os.path.join(folder, ".venv", "Scripts", "python.exe")):
        if os.path.isfile(path):
            return path
    return None


class MatteRelay:
    """trkar_matte.py を子プロセスで動かす。出力の最後の行を状態として見せる。"""

    def __init__(self):
        self._process = None
        self._stopped = set()
        self._lines = deque(maxlen=20)
        self.source = ""
        # ON にする前に Blender が受けていたソースと、受信していたか (止めたら戻す)
        self.previous_source = ""
        self.previous_running = False
        self.last_error = ""
        # 止めた時刻。止めた直後は NDI の一覧に自分の中継がしばらく残る
        self.stopped_at = 0.0

    @property
    def running(self):
        return self._process is not None and self._process.poll() is None

    @property
    def status(self):
        """中継が最後に出した行 (2 秒ごとの fps / 遅延など)"""
        return self._lines[-1] if self._lines else ""

    def start(self, folder, source, model="720"):
        self.stop()
        python = python_for(folder)
        if python is None:
            raise RuntimeError(_t("No .venv in %s (see the setup in tools/matte/README)") % folder)
        self._lines.clear()
        self.last_error = ""
        self.source = source
        self._process = subprocess.Popen(
            [python, "-u", os.path.join(folder, SCRIPT), "--source", source, "--model", model,
             "--name", OUTPUT_NAME, "--parent-pid", str(os.getpid())],
            cwd=folder, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )
        threading.Thread(target=self._read, args=(self._process,), daemon=True).start()

    def _read(self, process):
        for line in process.stdout:
            line = line.rstrip()
            if line:
                self._lines.append(line)
        code = process.wait()
        # 0 = 正常終了、-15 = SIGTERM (macOS/Linux)。Windows は terminate() が
        # TerminateProcess で即殺すので終了コードが 1 になり、シグナルは意味を持たない。
        # こちらから止めた (stop() を呼んだ) ときはどのコードでもエラー扱いしない
        if code not in (0, -15) and process not in self._stopped:
            self.last_error = self._lines[-1] if self._lines else _t("The relay exited (%d)") % code
        self._stopped.discard(process)

    def stop(self):
        process, self._process = self._process, None
        if process is None:
            return
        self.stopped_at = time.monotonic()
        if process.poll() is None:
            self._stopped.add(process)
            process.terminate()
            try:
                process.wait(3.0)
            except subprocess.TimeoutExpired:
                process.kill()
