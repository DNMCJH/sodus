"""Detached lifecycle for the HTTP daemon: `nodus up` / `nodus down`.

Why this exists: `serve` runs in the foreground, which means the board dies the
moment the terminal, the editor, or the agent session that launched it goes
away. That failure is silent — the page keeps loading from cache, the widget
shows "Nodus 没在跑", and nothing says who killed it. Every serious daemon has
an `up` that detaches; ours did not.

The state is a small JSON file next to the token, so a later invocation (or a
different shell) can find the process it started.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config

#: The child must outlive us: no console window, its own process group.
_DETACH = 0
if os.name == "nt":  # pragma: no cover - platform constants
    _DETACH = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

DEFAULT_PORT = 4199


def state_path() -> Path:
    return config.HOME / "daemon.json"


def log_path() -> Path:
    return config.HOME / "daemon.log"


def read_state() -> dict:
    """What we recorded last time we started a daemon. `{}` when unknown."""
    try:
        return json.loads(state_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def write_state(pid: int, port: int) -> dict:
    state = {"pid": int(pid), "port": int(port), "started": time.time()}
    state_path().write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    return state


def clear_state() -> None:
    try:
        state_path().unlink()
    except OSError:
        pass


def health(port: int, timeout: float = 1.0) -> bool:
    """Is a Nodus daemon answering on this port right now?"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return False


def health_info(port: int, timeout: float = 1.0) -> dict:
    """What the daemon on this port says about itself ({} when unreachable).

    The important field is `home`: a daemon serving a *different* state
    directory answers /health just fine, and every number on its board belongs
    to somebody else's work.
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{int(port)}/health", timeout=timeout) as resp:
            if resp.status != 200:
                return {}
            return json.loads(resp.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return {}


def same_home(info: dict) -> bool:
    """Is this daemon reading the same state directory as us?

    Older builds did not report `home`; treat that as "cannot prove it is a
    different one" so an upgrade does not lock the operator out.
    """
    remote = str((info or {}).get("home") or "")
    return (not remote) or remote == config.home_id()


def alive(pid: int) -> bool:
    """Is this pid a live process? (Cheap; does not prove it is *our* daemon.)"""
    if not pid:
        return False
    if os.name == "nt":  # pragma: no cover - platform branch
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"],
                capture_output=True, text=True, timeout=5, check=False,
                creationflags=config.NO_WINDOW,
            ).stdout
            return str(int(pid)) in out
        except (OSError, subprocess.SubprocessError):
            return False
    try:  # pragma: no cover - posix branch
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def status(port: int = DEFAULT_PORT) -> dict:
    """What a human wants to know: is it up, on which port, since when."""
    state = read_state()
    port = int(state.get("port") or port)
    up = health(port)
    info = health_info(port) if up else {}
    return {
        "up": up,
        "home_mismatch": bool(up) and not same_home(info),
        "remote_home": info.get("home") or "",
        "port": port,
        "pid": state.get("pid") if up else None,
        "started": state.get("started") if up else None,
        "url": f"http://127.0.0.1:{port}/" if up else "",
    }


def start(port: int = DEFAULT_PORT, wait: float = 20.0) -> dict:
    """Start the daemon detached and wait until it answers (or fail loudly).

    Returns status-shaped dicts with an `ok` key so the CLI can print one line
    either way. Never raises for the ordinary "already running" case.
    """
    current = status(port)
    if current["up"]:
        if current.get("home_mismatch"):
            return {
                **current,
                "ok": False,
                "error": (
                    f"port {port} is serving a different state directory "
                    f"(home {current.get('remote_home') or '?'} vs ours); refusing to "
                    "report it as ours - stop that daemon or pick another --port"
                ),
            }
        return {**current, "ok": True, "already": True}

    config.ensure_dirs()
    log = log_path()
    with open(log, "ab") as sink:
        proc = subprocess.Popen(
            [sys.executable, "-m", "nodus", "serve", "--port", str(int(port))],
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            creationflags=_DETACH,
            close_fds=True,
            cwd=str(config.HOME),
        )
    write_state(proc.pid, port)

    deadline = time.time() + wait
    while time.time() < deadline:
        if health(port):
            return {**status(port), "ok": True, "already": False}
        if proc.poll() is not None:
            clear_state()
            return {"ok": False, "error": f"daemon exited immediately (code {proc.returncode})",
                    "log": str(log)}
        time.sleep(0.25)
    return {"ok": False, "error": f"daemon did not answer within {wait:.0f}s", "log": str(log)}


def stop(port: int = DEFAULT_PORT) -> dict:
    """Stop whatever we started. Safe to call when nothing is running."""
    state = read_state()
    pid = int(state.get("pid") or 0)
    port = int(state.get("port") or port)
    if not pid:
        # No record, but something may still answer (started by hand / old
        # version): say so instead of pretending we stopped it.
        return {"ok": not health(port), "stopped": False,
                "note": "no pid recorded" + ("; something still listens" if health(port) else "")}
    if os.name == "nt":  # pragma: no cover - platform branch
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, text=True, check=False,
                       creationflags=config.NO_WINDOW)
    else:  # pragma: no cover - posix branch
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    deadline = time.time() + 10
    while time.time() < deadline and health(port):
        time.sleep(0.2)
    clear_state()
    return {"ok": not health(port), "stopped": True, "pid": pid}
