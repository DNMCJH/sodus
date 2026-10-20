"""HTTP API + SSE. One user, one machine: no auth beyond a loopback bind."""

from __future__ import annotations

import hmac
import json
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import agents, config, db, runner

MAX_BODY = 1_000_000


def authorized(header: str) -> bool:
    """Constant-time token check; a plain == leaks length/prefix timing."""
    return hmac.compare_digest(header or "", f"Bearer {config.token()}")


def body_allowed(length: int) -> bool:
    return 0 <= length <= MAX_BODY


def _json(handler: BaseHTTPRequestHandler, code: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _sse(handler: BaseHTTPRequestHandler, run_id: str | None, after: int) -> None:
    q = runner.BUS.subscribe()
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-cache")
    handler.send_header("Connection", "keep-alive")
    handler.end_headers()
    try:
        backlog = db.events_after(run_id, after) if run_id else []
        for ev in backlog:
            handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
        last_ping = time.time()
        while True:
            try:
                ev = q.get(timeout=15)
            except queue.Empty:
                if time.time() - last_ping > 15:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
                    last_ping = time.time()
                continue
            if run_id and ev.get("run_id") != run_id:
                continue
            handler.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
            handler.wfile.flush()
            last_ping = time.time()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        runner.BUS.unsubscribe(q)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        return

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _authorized(self) -> bool:
        if self.path.startswith("/health"):
            return True
        return authorized(self.headers.get("Authorization", ""))

    def do_GET(self) -> None:
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        path = u.path.rstrip("/") or "/"

        if path == "/health":
            return _json(self, 200, {"ok": True, "version": "0.1.0"})

        if path == "/agents":
            specs = agents.registry()
            secrets = config.load_secrets()
            return _json(
                self,
                200,
                [
                    {
                        "name": s.name,
                        "vendor": s.vendor,
                        "ready": not agents.missing_requirements(s, secrets),
                        "missing": agents.missing_requirements(s, secrets),
                    }
                    for s in specs.values()
                ],
            )

        if path == "/tasks":
            return _json(self, 200, db.list_tasks())

        if path.startswith("/tasks/"):
            return _json(self, 200, db.get_task(path.split("/")[2]))

        if path == "/runs":
            task_id = (qs.get("task_id") or [None])[0]
            runs = db.list_runs(task_id)
            states = db.verify_states([r["id"] for r in runs])
            for run in runs:
                run["verify"] = states.get(run["id"], "none")
            return _json(self, 200, runs)

        if path.startswith("/runs/"):
            parts = path.split("/")
            rid = parts[2]
            if len(parts) == 3:
                return _json(self, 200, db.get_run(rid))
            if parts[3] == "events":
                after = int((qs.get("after") or [0])[0])
                if (qs.get("stream") or ["0"])[0] in ("1", "true"):
                    return _sse(self, rid, after)
                return _json(self, 200, db.events_after(rid, after))
            if parts[3] == "artifacts":
                return _json(self, 200, db.artifacts_for(rid))
            if parts[3] == "handoff":
                h = db.latest_handoff(db.get_run(rid).get("task_id", ""))
                text = ""
                if h:
                    try:
                        text = Path(h["path"]).read_text(encoding="utf-8")
                    except OSError:
                        text = ""
                return _json(self, 200, {"path": h.get("path"), "text": text})

        if path == "/events":
            return _sse(self, None, 0)

        return _json(self, 404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return _json(self, 401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return _json(self, 400, {"error": "invalid content-length"})
        if not body_allowed(length):
            return _json(self, 413, {"error": "body too large"})
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        try:
            body = self._body()
        except json.JSONDecodeError:
            return _json(self, 400, {"error": "invalid json"})

        if path == "/tasks":
            task = db.create_task(
                goal=body.get("goal", ""),
                acceptance=body.get("acceptance", ""),
                session_id=body.get("session_id"),
                title=body.get("title", ""),
            )
            return _json(self, 201, task)

        if path == "/runs":
            try:
                run = runner.RUNNER.start(
                    task_id=body.get("task_id", ""),
                    agent_name=body.get("agent", "claude"),
                    cwd=body.get("cwd"),
                    parent_run_id=body.get("parent_run_id"),
                    note=body.get("note", ""),
                    effort=body.get("effort", ""),
                )
            except (ValueError, RuntimeError) as exc:
                return _json(self, 400, {"error": str(exc)})
            return _json(self, 201, run)

        if path.startswith("/runs/") and path.endswith("/stop"):
            rid = path.split("/")[2]
            return _json(self, 200, {"stopped": runner.RUNNER.stop(rid)})

        return _json(self, 404, {"error": "not found"})


def serve(port: int = 4199, host: str = "127.0.0.1") -> None:
    if host not in ("127.0.0.1", "localhost", "::1") and not os.environ.get(
        "NODUS_ALLOW_REMOTE"
    ):
        raise SystemExit("refusing non-loopback bind without NODUS_ALLOW_REMOTE=1")
    config.ensure_dirs()
    db.connect().close()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"nodusd listening on http://{host}:{port}", file=sys.stderr, flush=True)
    server.serve_forever()


def serve_async(port: int = 4199) -> threading.Thread:
    t = threading.Thread(target=serve, args=(port,), daemon=True)
    t.start()
    return t
