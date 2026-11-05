# SPDX-License-Identifier: GPL-3.0-or-later  (tools/matte/LICENSE、trkAR Camera と同じ)
"""trkAR の実写 NDI を Robust Video Matting (RVM) で切り抜いて、アルファ付きの NDI で送り直す。

    iPhone ─NDI "trkAR"─▶ このプログラム (RVM) ─NDI "trkAR Matte" (RGBA)─▶ Blender / OBS

色は受けた映像のまま、アルファに RVM の人物マット (髪の毛まで半透明) を入れる。
timecode は受けた映像のものを引き継ぐので、Blender の「映像に合わせる」(深度の同期) はそのまま効く。

推論は 2 通り (README 参照):
    - Core ML (macOS): models/*.mlmodel
    - ONNX Runtime (Windows / Linux、DirectML か CUDA か CPU): models/*.onnx
既定は macOS なら Core ML、それ以外は ONNX Runtime (`--backend` で明示もできる)。

使い方:
    .venv/bin/python trkar_matte.py --list                 # NDI ソース一覧
    .venv/bin/python trkar_matte.py --source "IPHONE (trkAR)"
    .venv/bin/python trkar_matte.py                        # 名前に "(trkAR)" を含む最初のソース

解像度は固定 (720 / 1080) なので、受けた映像は縦横比を保ってその大きさに収め (余りは黒)、
結果を元の大きさに戻す。縦位置 (9:16) の映像もそのまま扱える (横長の中央に小さく入る)。
"""

import argparse
import ctypes
import os
import signal
import sys
import threading
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
# Blender アドオンの ndi.py を使う。開発のリポジトリは blender/trkar_camera、公開のリポジトリは addon
for _addon in (os.path.join(HERE, "..", "..", "blender", "trkar_camera"), os.path.join(HERE, "..", "..", "addon")):
    if os.path.isfile(os.path.join(_addon, "ndi.py")):
        sys.path.insert(0, _addon)
        break
import ndi  # noqa: E402  (Blender アドオンの ctypes ラッパー。bpy には依存しない)

# resolution: 受信映像を収める大きさ / ratio: RVM の downsample_ratio (小さいほど細かいが遅い)
MODEL_CONFIG = {
    "720": {"resolution": (1280, 720), "ratio": 0.375, "coreml": "rvm_mobilenetv3_1280x720_s0.375_fp16.mlmodel"},
    "1080": {"resolution": (1920, 1080), "ratio": 0.25, "coreml": "rvm_mobilenetv3_1920x1080_s0.25_fp16.mlmodel"},
}
ONNX_MODEL = "rvm_mobilenetv3_fp32.onnx"


class Latest:
    """受信スレッドが最新の 1 フレームだけを置いておく (処理が遅れたら古いものは捨てる)。"""

    def __init__(self):
        self._cond = threading.Condition()
        self._item = None
        self.received = 0
        self.dropped = 0

    def put(self, item):
        with self._cond:
            if self._item is not None:
                self.dropped += 1
            self._item = item
            self.received += 1
            self._cond.notify()

    def take(self, timeout):
        with self._cond:
            if self._item is None:
                self._cond.wait(timeout)
            item, self._item = self._item, None
            return item


def receive_loop(receiver, latest, running):
    def copy(frame):
        width, height, stride = frame.xres, frame.yres, frame.line_stride_in_bytes
        buf = (ctypes.c_ubyte * (stride * height)).from_address(frame.p_data)
        pixels = np.frombuffer(buf, np.uint8).reshape(height, stride)[:, :width * 4].reshape(height, width, 4)
        pixels = ndi.to_rgba_order(pixels, frame.FourCC)
        rate = frame.frame_rate_N / frame.frame_rate_D if frame.frame_rate_D else 30.0
        latest.put((pixels.copy(), frame.timecode, rate, time.perf_counter()))

    while running.is_set():
        try:
            receiver.capture(copy, 100)
        except RuntimeError as exc:
            print("受信エラー:", exc)
            time.sleep(0.5)



def send_loop(sender, outgoing, running, stats):
    """RVM と並行して送る (NDI の RGBA 送信は 1080p で数 ms かかるので、その間に次を切り抜く)。"""
    while running.is_set():
        item = outgoing.take(0.2)
        if item is None:
            continue
        rgba, timecode, rate, received_at = item
        sender.send_rgba(rgba, rgba.shape[1], rgba.shape[0], rate, True, timecode)
        stats["relay_ms"].append((time.perf_counter() - received_at) * 1000)
        stats["sent"] += 1


class _MatteBase:
    """縦横比を保って作業解像度に収め、余白を付けて推論し、元の大きさに戻す (共通部分)。"""

    def _prepare(self, rgba):
        height, width = rgba.shape[:2]
        mw, mh = self.size
        scale = min(mw / width, mh / height)
        sw, sh = max(1, round(width * scale)), max(1, round(height * scale))
        ox, oy = (mw - sw) // 2, (mh - sh) // 2
        if self._key != (width, height):
            # 大きさが変わったら前のフレームの記憶は使えない
            self._key = (width, height)
            self.state = self._initial_state()
        image = Image.fromarray(rgba[..., :3])
        if (sw, sh) != (width, height):
            image = image.resize((sw, sh), Image.BILINEAR)
        if (sw, sh) != (mw, mh):
            canvas = Image.new("RGB", (mw, mh))
            canvas.paste(image, (ox, oy))
            image = canvas
        return image, (width, height), (sw, sh), (ox, oy)

    def _restore(self, pha, size, resized, offset):
        width, height = size
        sw, sh = resized
        ox, oy = offset
        if (sw, sh) != self.size:
            pha = pha.crop((ox, oy, ox + sw, oy + sh))
        if (sw, sh) != (width, height):
            pha = pha.resize((width, height), Image.BILINEAR)
        return np.asarray(pha, np.uint8)


class MatteCoreML(_MatteBase):
    def __init__(self, path, compute_units):
        import coremltools as ct
        self.model = ct.models.MLModel(path, compute_units=getattr(ct.ComputeUnit, compute_units))
        spec = self.model.get_spec()
        src = next(i for i in spec.description.input if i.name == "src").type.imageType
        self.size = (src.width, src.height)
        self.recurrent = [i.name for i in spec.description.input if i.name != "src"]
        self._key = None
        self.state = self._initial_state()

    def _initial_state(self):
        return {}

    def __call__(self, rgba):
        """rgba (h, w, 4) uint8 → アルファ (h, w) uint8"""
        image, size, resized, offset = self._prepare(rgba)
        out = self.model.predict({"src": image, **self.state})
        self.state = {name: out[name[:-1] + "o"] for name in self.recurrent}
        return self._restore(out["pha"], size, resized, offset)


class MatteONNX(_MatteBase):
    def __init__(self, path, resolution, ratio, providers):
        import onnxruntime as ort
        self.session = ort.InferenceSession(path, providers=providers)
        self.size = resolution
        self.ratio = ratio
        input_names = {i.name for i in self.session.get_inputs()}
        self.recurrent = sorted(input_names - {"src", "downsample_ratio"})
        self.output_names = [o.name for o in self.session.get_outputs()]
        self._key = None
        self.state = self._initial_state()

    def _initial_state(self):
        # RVM の ONNX モデルは最初のフレームだけ 1x1x1x1 のゼロで足りる (内部でブロードキャストされる)
        return {name: np.zeros((1, 1, 1, 1), np.float32) for name in self.recurrent}

    def __call__(self, rgba):
        """rgba (h, w, 4) uint8 → アルファ (h, w) uint8"""
        image, size, resized, offset = self._prepare(rgba)
        src = np.asarray(image, np.float32).transpose(2, 0, 1)[None] / 255.0
        inputs = {"src": src, "downsample_ratio": np.array([self.ratio], np.float32), **self.state}
        result = dict(zip(self.output_names, self.session.run(self.output_names, inputs)))
        self.state = {name: result[name[:-1] + "o"] for name in self.recurrent}
        pha = Image.fromarray((result["pha"][0, 0] * 255).clip(0, 255).astype(np.uint8))
        return self._restore(pha, size, resized, offset)


def find_source(wanted, wait=5.0):
    finder = ndi.Finder()
    try:
        deadline = time.time() + wait
        while True:
            sources = finder.sources()
            if wanted is None:
                match = [s for s in sources if "(trkAR)" in s]
            else:
                match = [s for s in sources if s == wanted] or [s for s in sources if wanted in s]
            if match or time.time() > deadline:
                return match[0] if match else None, sources
            time.sleep(0.3)
    finally:
        finder.close()


def _exit_on_term(*_args):
    raise SystemExit(0)


def _parent_alive(pid):
    """`--parent-pid` の生死を見る (Windows は PPID が親の終了で変わらないため getppid 比較が効かない)。"""
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            STILL_ACTIVE = 259
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _onnx_providers(choice):
    import onnxruntime as ort
    if choice != "auto":
        table = {"cpu": "CPUExecutionProvider", "dml": "DmlExecutionProvider", "cuda": "CUDAExecutionProvider"}
        return [table[choice], "CPUExecutionProvider"]
    available = ort.get_available_providers()
    order = ["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"]
    return [p for p in order if p in available] or ["CPUExecutionProvider"]


def load_matte(backend, model_key, units, onnx_provider):
    config = MODEL_CONFIG[model_key]
    if backend == "coreml":
        path = os.path.join(HERE, "models", config["coreml"])
        if not os.path.exists(path):
            sys.exit("モデルがありません: %s (README の手順で入れる)" % path)
        print("読み込み中: %s (Core ML, %s)" % (config["coreml"], units))
        return MatteCoreML(path, units)
    path = os.path.join(HERE, "models", ONNX_MODEL)
    if not os.path.exists(path):
        sys.exit("モデルがありません: %s (README の手順で入れる)" % path)
    providers = _onnx_providers(onnx_provider)
    print("読み込み中: %s (ONNX Runtime, %s)" % (ONNX_MODEL, providers[0]))
    return MatteONNX(path, config["resolution"], config["ratio"], providers)


def main():
    signal.signal(signal.SIGTERM, _exit_on_term)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", help='受ける NDI ソース (例 "IPHONE (trkAR)"。一部だけでも可)')
    parser.add_argument("--name", default="trkAR Matte", help="送り出す NDI の名前")
    parser.add_argument("--model", choices=sorted(MODEL_CONFIG), default="720",
                        help="720 = 速い (M4 で約 17ms) / 1080 = 細かい (約 25ms)")
    parser.add_argument("--backend", default="auto", choices=["auto", "coreml", "onnx"],
                        help="推論エンジン (既定: macOS は coreml、それ以外は onnx)")
    parser.add_argument("--units", default="CPU_AND_NE", choices=["CPU_AND_NE", "ALL", "CPU_AND_GPU"],
                        help="Core ML の計算装置 (M4 では CPU_AND_NE が一番速い)")
    parser.add_argument("--onnx-provider", default="auto", choices=["auto", "cuda", "dml", "cpu"],
                        help="ONNX Runtime の実行先 (既定: NVIDIA GPU (cuda) → DirectML (dml) → CPU の順で使えるものを選ぶ)")
    parser.add_argument("--list", action="store_true", help="NDI ソースを一覧して終わる")
    parser.add_argument("--parent-pid", type=int, help="このプロセスが終わったら一緒に終わる (Blender から起動したとき)")
    args = parser.parse_args()

    if args.list:
        _, sources = find_source("\0", wait=3.0)
        print("\n".join(sources) if sources else "(NDI ソースが見つかりません)")
        return

    backend = args.backend if args.backend != "auto" else ("coreml" if sys.platform == "darwin" else "onnx")
    source, sources = find_source(args.source)
    if source is None:
        sys.exit("NDI ソースが見つかりません。見えているもの: %s" % (sources or "なし"))

    matte = load_matte(backend, args.model, args.units, args.onnx_provider)
    matte(np.zeros((matte.size[1], matte.size[0], 4), np.uint8))  # 初回のコンパイル/最適化を先に済ませる
    matte.state = matte._initial_state()

    receiver = ndi.Receiver(source)
    sender = ndi.Sender(args.name)
    latest = Latest()
    outgoing = Latest()
    stats = {"sent": 0, "relay_ms": []}
    running = threading.Event()
    running.set()
    threads = [
        threading.Thread(target=receive_loop, args=(receiver, latest, running), daemon=True),
        threading.Thread(target=send_loop, args=(sender, outgoing, running, stats), daemon=True),
    ]
    for thread in threads:
        thread.start()
    print("%s → RVM → NDI \"%s\"  (Ctrl+C で終了)" % (source, args.name))

    infer_ms = []
    last_report = time.perf_counter()
    last_received = 0
    slow_warned = False
    try:
        while True:
            item = latest.take(0.5)
            now = time.perf_counter()
            if item is not None:
                rgba, timecode, rate, received_at = item
                t0 = time.perf_counter()
                alpha = matte(rgba)
                infer_ms.append((time.perf_counter() - t0) * 1000)
                rgba[..., 3] = alpha
                outgoing.put((rgba, timecode, rate, received_at))
            if args.parent_pid and not _parent_alive(args.parent_pid):
                break  # 起動した Blender が落ちた
            if now - last_report >= 2.0:
                span = now - last_report
                fps_in = (latest.received - last_received) / span
                if infer_ms:
                    median_ms = np.median(infer_ms)
                    print("受信 %.1f fps / 送出 %.1f fps / RVM %.1f ms / 受信→送出 %.1f ms / 間引き %d / 受け手 %d" % (
                        fps_in, stats["sent"] / span, median_ms,
                        np.median(stats["relay_ms"] or [0]), latest.dropped + outgoing.dropped,
                        sender.connections()))
                    if not slow_warned and median_ms > 100:
                        slow_warned = True
                        print("※ RVM が %.0fms/フレームかかっていて (30fps には 33ms 以下が必要)、この GPU では"
                              " リアルタイムに向きません。切り抜きをやめて ARKit の人物マスクでの切り抜き"
                              " (Blender の深度スクリーン、既定の「人の輪郭で切る」) の利用を検討してください" % median_ms)
                else:
                    print("映像を待っています… (%s)" % source)
                last_report, last_received = now, latest.received
                stats["sent"] = 0
                infer_ms.clear()
                stats["relay_ms"].clear()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        running.clear()
        for thread in threads:
            thread.join(1.0)
        receiver.close()
        sender.close()


if __name__ == "__main__":
    main()
