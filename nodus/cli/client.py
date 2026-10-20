"""HTTP client used by the CLI: one place that knows the daemon exists."""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request

from .. import config

BASE = "http://127.0.0.1:4199"


def alive(port: int = 4199) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_daemon(port: int = 4199) -> None:
    from .. import server

    if alive(port):
        return
    server.serve_async(port)
    for _ in range(50):
        if alive(port):
            return
        time.sleep(0.1)
    raise RuntimeError("nodusd failed to start")


def call(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.token()}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"daemon unreachable: {e}") from None


def fmt_event(ev: dict) -> str | None:
    t = ev["type"]
    p = ev["payload"]
    if t == "session":
        return f"  · session {p.get('session_id')} model={p.get('model', '?')}"
    if t == "message":
        return p.get("text", "")
    if t == "thinking":
        return f"  ~ thinking ({len(p.get('text', ''))} chars)"
    if t == "tool_call":
        inp = json.dumps(p.get("input"), ensure_ascii=False)
        return f"  → {p.get('name')} {inp[:200]}"
    if t == "tool_result":
        flag = "ok" if p.get("ok") else "ERR"
        return f"  ← {flag} {(p.get('text') or '')[:160]}"
    if t == "error":
        return f"  ! {p.get('message')}"
    if t in ("run.completed", "run.failed"):
        u = p.get("usage") or {}
        return (
            f"\n[{p.get('status')}] exit={p.get('exit_code')} "
            f"in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)} "
            f"cache_read={u.get('cache_read', 0)}\nhandoff: {p.get('handoff')}"
        )
    return None


def watch(rid: str, after: int = 0) -> int:
    seen = after
    while True:
        run = call("GET", f"/runs/{rid}")
        for ev in call("GET", f"/runs/{rid}/events?after={seen}"):
            seen = max(seen, ev["seq"])
            line = fmt_event(ev)
            if line is not None:
                print(line, flush=True)
        if run.get("status") in ("completed", "failed"):
            return 0 if run["status"] == "completed" else 1
        time.sleep(0.5)
