"""HTTP client used by the CLI: one place that knows the daemon exists."""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from .. import config
from ..daemon import write_state

BASE = "http://127.0.0.1:4199"
REPO_ROOT = Path(__file__).resolve().parents[2]


def alive(port: int = 4199) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_daemon(port: int = 4199) -> None:
    """Start the daemon as its own process, never inside this one.

    An in-process daemon used to die with whichever CLI happened to start it:
    with four concurrent `nodus run`, the first one to finish took the daemon
    (and the other three watchers) down with it. The daemon outlives every
    client, so it has to be a separate process.

    The pid we spawn is written to `daemon.json`, the state `nodus down` reads.
    Before this, a daemon started by this path could only be stopped by hand:
    `nodus down` found no record and the next `nodus up` reported a pid that
    was already dead.
    """
    if alive(port):
        return
    config.ensure_dirs()
    log_path = config.HOME / "daemon.log"
    log = log_path.open("a", encoding="utf-8", errors="replace")
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", _SERVE_SNIPPET, str(port)],
            cwd=str(REPO_ROOT),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not start nodusd: {exc}") from None
    finally:
        log.close()
    for _ in range(60):
        if alive(port):
            write_state(proc.pid, port)
            return
        time.sleep(0.1)
    raise RuntimeError(f"nodusd failed to start; see {log_path}")


_SERVE_SNIPPET = (
    "import sys; from nodus.cli import main; sys.exit(main(['serve', '--port', sys.argv[1]]))"
)


def call(method: str, path: str, body: dict | None = None, retries: int = 2, *, headers: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.token()}",
            **(headers or {}),
        },
    )
    attempt = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, ConnectionResetError, ConnectionAbortedError) as e:
            # A dropped connection is normal when several clients poll at once;
            # a read-only call is safe to repeat.
            if method == "GET" and attempt < retries:
                attempt += 1
                time.sleep(0.2 * attempt)
                continue
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
    if t == "run.attempt":
        return f"  · attempt {p.get('attempt')} on {p.get('provider')} ({p.get('model') or '-'})"
    if t == "run.fallback":
        # Never silent: the fallback endpoint is a different, often weaker model
        return (
            f"  !! {p.get('from')} failed before doing anything"
            f" ({p.get('reason')}) → retrying on {p.get('to')}"
        )
    if t == "verify.started":
        return f"  · verify: {' '.join(str(c) for c in p.get('cmd') or [])} ({p.get('reason')})"
    if t in ("verify.green", "verify.red"):
        flag = "PASS" if p.get("ok") else "FAIL"
        return f"  · verify {p.get('phase')}: {flag} exit={p.get('exit')} ({p.get('duration_ms')}ms)"
    if t in ("run.completed", "run.failed"):
        u = p.get("usage") or {}
        money = ""
        if isinstance(u.get("cost"), (int, float)):
            symbol = "¥" if u.get("currency") == "CNY" else f"{u.get('currency')} "
            money = f" cost={symbol}{u['cost']:.6f}({u.get('cost_basis')})"
        return (
            f"\n[{p.get('status')}] provider={p.get('provider', '-')} exit={p.get('exit_code')} "
            f"in={u.get('input_tokens', 0)} out={u.get('output_tokens', 0)} "
            f"cache_read={u.get('cache_read', 0)}{money}\nhandoff: {p.get('handoff')}"
        )
    return None


def watch(rid: str, after: int = 0) -> int:
    seen = after
    restarts = 0
    while True:
        try:
            run = call("GET", f"/runs/{rid}")
            for ev in call("GET", f"/runs/{rid}/events?after={seen}"):
                seen = max(seen, ev["seq"])
                line = fmt_event(ev)
                if line is not None:
                    print(line, flush=True)
            if run.get("status") in ("completed", "failed"):
                return 0 if run["status"] == "completed" else 1
        except (TimeoutError, ConnectionError, http.client.HTTPException):
            # A long tool call can leave the event connection idle past the
            # socket timeout. That is a quiet run, not a dead one: reconnect
            # with the last seen sequence and keep watching.
            time.sleep(0.5)
            continue
        except RuntimeError as exc:
            # The daemon can be restarted underneath us. Come back once; the
            # fresh daemon reaps the orphaned run, so this loop still ends.
            if "unreachable" not in str(exc) or restarts >= 2:
                raise
            restarts += 1
            time.sleep(0.5)
            ensure_daemon()
            continue
        time.sleep(0.5)
