"""テイクのあとに、ユーザーが決めたコマンドを流す (クラウドや別のツールにつなぐ口)。

trkAR 自身はクラウドに送らない。S3・Google Drive・社内の NAS などへの連携は、このコマンドとして
ユーザーが用意する (仕様は INTEGRATION.md)。

- イベント
    recorded  録画を止めたとき (take.json・actions.json・video/ などが揃ったあと)
    exported  書き出しが終わったとき (render.mp4 か render/ ができたあと。take.json に render が足してある)
- コマンドは 1 行の文字列。{take_dir} {event} {render} {take_json} を置き換えてから shlex で分けて実行する
  (シェルは通さない。パイプやリダイレクトを使うならスクリプトに書く)。作業フォルダはテイクのフォルダ
- 環境変数 TRKAR_HOOK_VERSION / TRKAR_EVENT / TRKAR_TAKE_DIR / TRKAR_TAKE_JSON / TRKAR_RENDER /
  TRKAR_BLEND / TRKAR_ADDON_VERSION を足して渡す
- 標準出力・標準エラーは <テイク>/hook.log に追記。結果は <テイク>/hook.json に残す
  ({"event", "exit_code", "started_utc", "finished_utc", "seconds", "message", "url"})
- 終了コード 0 が成功。標準出力の最後の行が JSON のオブジェクトなら、その message / url をパネルに出す
- 一度に 1 つずつ、来た順に流す (別スレッド)。結果はタイマー (メインスレッド) で受ける
"""

import datetime
import json
import os
import queue
import shlex
import subprocess
import sys
import threading
import time

import bpy

HOOK_VERSION = 1
STATE_FILE = "hook.json"
LOG_FILE = "hook.log"
EVENTS = ("recorded", "exported")
# Finder / Dock から起動した Blender は PATH が短い (python3 や aws を探せるように足す)
_EXTRA_PATH = ("/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin"))

# テイクのパス -> {"state": "queued" | "running" | "done" | "failed", "event", "message", "url"}
_status = {}
_jobs = queue.Queue()
_results = queue.Queue()
_lock = threading.Lock()
_worker = None


def _utc():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _render_path(path):
    video = os.path.join(path, "render.mp4")
    if os.path.isfile(video):
        return video
    frames = os.path.join(path, "render")
    return frames if os.path.isdir(frames) else ""


def build_command(template, path, event):
    """コマンドの文字列を、置き換えてから引数の並びにする。空なら None"""
    template = (template or "").strip()
    if not template:
        return None
    values = {"take_dir": path, "event": event, "render": _render_path(path),
              "take_json": os.path.join(path, "take.json")}
    args = shlex.split(template, posix=sys.platform != "win32")
    return [os.path.expanduser(arg.format(**values)) for arg in args]


def _child_env(path, event, addon_version):
    env = dict(os.environ)
    parts = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(parts + [p for p in _EXTRA_PATH if p not in parts])
    env.update({
        "TRKAR_HOOK_VERSION": str(HOOK_VERSION),
        "TRKAR_EVENT": event,
        "TRKAR_TAKE_DIR": path,
        "TRKAR_TAKE_JSON": os.path.join(path, "take.json"),
        "TRKAR_RENDER": _render_path(path),
        "TRKAR_BLEND": bpy.data.filepath,
        "TRKAR_ADDON_VERSION": addon_version or "",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def status(path):
    """パネル用。動かしていなければ前回の hook.json"""
    with _lock:
        st = _status.get(path)
    if st is not None:
        return st
    try:
        with open(os.path.join(path, STATE_FILE), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return None
    return {"state": "done" if saved.get("exit_code") == 0 else "failed", "event": saved.get("event", ""),
            "message": saved.get("message", ""), "url": saved.get("url", "")}


def busy(path=None):
    with _lock:
        if path is None:
            return any(st["state"] in ("queued", "running") for st in _status.values())
        st = _status.get(path)
        return st is not None and st["state"] in ("queued", "running")


def label(st):
    event = {"recorded": "録画", "exported": "書き出し"}.get(st.get("event"), st.get("event", ""))
    if st["state"] == "queued":
        return "連携: 待ち (%s)" % event, "TIME"
    if st["state"] == "running":
        return "連携: 実行中 (%s)" % event, "TIME"
    if st["state"] == "done":
        return "連携: 済み (%s) %s" % (event, st.get("message", "")), "CHECKMARK"
    return "連携: 失敗 (%s) %s" % (event, st.get("message", "")), "ERROR"


def run(path, event, template, addon_version="", timeout_min=30):
    """コマンドを流す順番に入れる。コマンドが空なら何もしない (False)"""
    try:
        cmd = build_command(template, path, event)
    except (ValueError, KeyError, IndexError) as exc:
        _set(path, {"state": "failed", "event": event, "message": "コマンドが読めません: %s" % exc, "url": ""})
        return False
    if cmd is None:
        return False
    _set(path, {"state": "queued", "event": event, "message": "", "url": ""})
    _jobs.put((path, event, cmd, _child_env(path, event, addon_version), max(1, timeout_min) * 60))
    _ensure_worker()
    if not bpy.app.timers.is_registered(_poll):
        bpy.app.timers.register(_poll, first_interval=0.5, persistent=True)
    return True


def _set(path, st):
    with _lock:
        _status[path] = st


def _ensure_worker():
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_work, name="trkAR hook", daemon=True)
        _worker.start()


def _log(path, text):
    try:
        with open(os.path.join(path, LOG_FILE), "a", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


def _work():
    while True:
        path, event, cmd, env, timeout = _jobs.get()
        started, t0 = _utc(), time.monotonic()
        _set(path, {"state": "running", "event": event, "message": "", "url": ""})
        _log(path, "\n=== %s %s: %s\n" % (started, event, " ".join(shlex.quote(c) for c in cmd)))
        code, out = None, ""
        try:
            proc = subprocess.run(cmd, cwd=path, env=env, capture_output=True, text=True, timeout=timeout)
            code, out = proc.returncode, proc.stdout or ""
            _log(path, out + (proc.stderr or ""))
        except subprocess.TimeoutExpired:
            _log(path, "(%d 分で打ち切り)\n" % (timeout // 60))
            out = json.dumps({"message": "%d 分で打ち切り" % (timeout // 60)})
        except OSError as exc:
            _log(path, "起動できません: %s\n" % exc)
            out = json.dumps({"message": "起動できません: %s" % exc})
        _results.put((path, event, code, out, started, time.monotonic() - t0))


def _last_json(out):
    for line in reversed((out or "").strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return {}


def _poll():
    while True:
        try:
            path, event, code, out, started, seconds = _results.get_nowait()
        except queue.Empty:
            break
        result = _last_json(out)
        message = str(result.get("message", "")) or ("" if code == 0 else "終了コード %s" % code)
        url = str(result.get("url", ""))
        _set(path, {"state": "done" if code == 0 else "failed", "event": event, "message": message, "url": url})
        try:
            with open(os.path.join(path, STATE_FILE), "w", encoding="utf-8") as f:
                json.dump({"event": event, "exit_code": code, "started_utc": started, "finished_utc": _utc(),
                           "seconds": round(seconds, 1), "message": message, "url": url},
                          f, ensure_ascii=False, indent=2)
        except OSError:
            pass
    return 0.5 if busy() or not _results.empty() else None


def stop():
    """アドオンを外すとき。動いているコマンドは最後まで流れる (結果は受けない)"""
    if bpy.app.timers.is_registered(_poll):
        bpy.app.timers.unregister(_poll)
