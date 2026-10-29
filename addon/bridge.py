"""通信ポート (自動化): 外のツール (Griptape Nodes のノード、Claude などのエージェント、自作のスクリプト) から
trkAR Camera を動かす (127.0.0.1 の HTTP)。命令の一覧は公開リポジトリの INTEGRATION.md の 3 章。

127.0.0.1 だけで HTTP を受け、決まった命令だけを Blender のメインスレッドで実行する
(BlenderMCP のように任意の Python は受けない)。命令の中身は __init__ が handlers で渡す。

    POST /command  {"command": "state", "args": {}}
      → {"ok": true, "result": {...}}  /  {"ok": false, "error": "..."}
    GET  /state    state の短縮

命令が終わるまで HTTP の返事を待たせる。書き出しのように長いものは受け付けたら返し、
進み具合は state で見る。
"""
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bpy

DEFAULT_PORT = 9310
# メインスレッドの返事を待つ上限 (シーンを開くのに時間がかかることがある)
WAIT_SECONDS = 120.0

_pending = queue.Queue()
_state = {"server": None, "thread": None, "handlers": {}, "context": None, "last": ""}


class _Job:
    def __init__(self, command, args):
        self.command = command
        self.args = args
        self.done = threading.Event()
        self.reply = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") == "/state":
            self._send(200, _run("state", {}))
        else:
            self._send(404, {"ok": False, "error": "GET は /state だけ"})

    def do_POST(self):
        if self.path.rstrip("/") != "/command":
            self._send(404, {"ok": False, "error": "POST は /command だけ"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            command = body["command"]
            args = body.get("args") or {}
            if not isinstance(args, dict):
                raise ValueError("args は object")
        except (ValueError, KeyError, TypeError) as exc:
            self._send(400, {"ok": False, "error": "読めない命令: %s" % exc})
            return
        self._send(200, _run(command, args))


def _run(command, args):
    if command not in _state["handlers"]:
        return {"ok": False, "error": "知らない命令: %s (使えるのは %s)" % (
            command, ", ".join(sorted(_state["handlers"])))}
    job = _Job(command, args)
    _pending.put(job)
    if not job.done.wait(WAIT_SECONDS):
        return {"ok": False, "error": "Blender が %d 秒返事をしません" % WAIT_SECONDS}
    return job.reply


def _override():
    """タイマーには窓がないので、最初の窓と 3D ビューを渡す (書き出しが 3D ビューを要る)"""
    wm = bpy.context.window_manager
    if wm is None or not wm.windows:
        return {}
    window = wm.windows[0]
    kwargs = {"window": window, "screen": window.screen}
    for area in window.screen.areas:
        if area.type == "VIEW_3D":
            kwargs["area"] = area
            kwargs["region"] = next((r for r in area.regions if r.type == "WINDOW"), None)
            break
    return kwargs


def _pump():
    while True:
        try:
            job = _pending.get_nowait()
        except queue.Empty:
            break
        try:
            with bpy.context.temp_override(**_override()):
                result = _state["handlers"][job.command](bpy.context, **job.args)
            job.reply = {"ok": True, "result": result}
        except TypeError as exc:
            job.reply = {"ok": False, "error": "引数が合いません: %s" % exc}
        except Exception as exc:
            job.reply = {"ok": False, "error": str(exc)}
        _state["last"] = "%s: %s" % (job.command, "OK" if job.reply["ok"] else job.reply["error"])
        job.done.set()
    return 0.1


def start(handlers, port=DEFAULT_PORT):
    stop()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, name="trkar-bridge", daemon=True)
    thread.start()
    _state.update(server=server, thread=thread, handlers=dict(handlers), last="")
    if not bpy.app.timers.is_registered(_pump):
        # シーンを開いても止まらないように persistent
        bpy.app.timers.register(_pump, persistent=True)


def stop():
    server = _state["server"]
    if server is not None:
        server.shutdown()
        server.server_close()
    _state.update(server=None, thread=None)
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)
    # 待っている命令には断りを返す
    while True:
        try:
            job = _pending.get_nowait()
        except queue.Empty:
            break
        job.reply = {"ok": False, "error": "通信ポートを閉じました"}
        job.done.set()


def running():
    return _state["server"] is not None


def port():
    server = _state["server"]
    return server.server_address[1] if server is not None else 0


def last():
    return _state["last"]
