"""更新と描画の時間を測る (コマ落ち・ふわふわの原因を見るため)。

- 更新 (カメラ・実写・深度のタイマー): 1 回あたりの時間と、1 秒に何回呼ばれたか
- 描画: 3D ビューが実際に何回描かれたか (描画の間隔)。カメラの更新が描画より少なければコマ落ちになる

パネルの「詳細」に出す。環境変数 TRKAR_PERF_LOG にファイルのパスを入れると、2 秒ごとにそこへ JSON で書く
(Blender の外から測るとき)。
"""

import collections
import json
import os
import time

import bpy

WINDOW = 3.0  # 直近この秒数で数える

_sections = {}
_draws = collections.deque()
_handle = None
_log_at = 0.0


def add(name, seconds):
    """name の更新に seconds かかった (タイマーの中から呼ぶ)"""
    now = time.perf_counter()
    samples = _sections.setdefault(name, collections.deque())
    samples.append((now, seconds))
    _trim(samples, now)


def _trim(samples, now):
    while samples and now - samples[0][0] > WINDOW:
        samples.popleft()


def _on_draw():
    now = time.perf_counter()
    _draws.append(now)
    _trim_draws(now)
    _maybe_log(now)


def _trim_draws(now):
    while _draws and now - _draws[0] > WINDOW:
        _draws.popleft()


def start():
    """3D ビューの描画を数え始める (受信を始めたとき)"""
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_on_draw, (), "WINDOW", "POST_PIXEL")


def stop():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, "WINDOW")
        _handle = None
    _sections.clear()
    _draws.clear()


def _percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * (len(values) - 1) + 0.5))] if values else 0.0


def stats():
    """{"draw": {"fps", "interval_ms", "interval_p95_ms"}, "<更新の名前>": {"per_s", "ms", "p95_ms", "max_ms"}}。
    3D ビューが複数あると描画はビューの数だけ数える"""
    now = time.perf_counter()
    _trim_draws(now)
    result = {}
    draws = list(_draws)
    intervals = [(b - a) * 1000.0 for a, b in zip(draws, draws[1:])]
    span = (draws[-1] - draws[0]) if len(draws) > 1 else 0.0
    result["draw"] = {"fps": (len(draws) - 1) / span if span > 0 else 0.0,
                      "interval_ms": sum(intervals) / len(intervals) if intervals else 0.0,
                      "interval_p95_ms": _percentile(intervals, 0.95)}
    for name, samples in _sections.items():
        _trim(samples, now)
        durations = [s * 1000.0 for _, s in samples]
        result[name] = {"per_s": len(samples) / WINDOW,
                        "ms": sum(durations) / len(durations) if durations else 0.0,
                        "p95_ms": _percentile(durations, 0.95),
                        "max_ms": max(durations) if durations else 0.0}
    return result


def lines():
    """パネルに出す行"""
    s = stats()
    draw = s.pop("draw")
    out = ["描画 %.0f fps (間隔 %.0f ms、遅いとき %.0f ms)" % (draw["fps"], draw["interval_ms"], draw["interval_p95_ms"])]
    labels = {"tracking": "カメラ", "ndi_in": "実写", "depth": "深度"}
    for name in ("tracking", "ndi_in", "depth"):
        if name in s:
            v = s[name]
            out.append("%s %.0f 回/秒 (1 回 %.1f ms、遅いとき %.1f ms)" % (labels[name], v["per_s"], v["ms"], v["p95_ms"]))
    return out


def _maybe_log(now):
    global _log_at
    path = os.environ.get("TRKAR_PERF_LOG")
    if not path or now - _log_at < 2.0:
        return
    _log_at = now
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), **stats()}) + "\n")
    except OSError:
        pass
