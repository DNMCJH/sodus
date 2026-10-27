"""Keep harness observations current without a person running --import.

observe.py only reads DSH's session files when someone types
`nodus debug observe dsh --import`. That is a gap in the
ledger: the board can show money it knew about days ago while today's
conversation sits unread on disk. The daemon already owns a clock for recovery
and reaping, so it sweeps the same store on a timer; the per-file cursor in
observe.scan() makes a sweep cheap once a file has been seen.

Two knobs, both environment variables:

    NODUS_OBSERVE_SYNC=0             turn the sweep off
    NODUS_OBSERVE_SYNC_SECONDS=300   how often to look (default 300)
"""

from __future__ import annotations

import os
import threading
import time

from . import observe

DEFAULT_SECONDS = 300.0


def enabled() -> bool:
    return os.environ.get("NODUS_OBSERVE_SYNC", "1") != "0"


def interval() -> float:
    raw = os.environ.get("NODUS_OBSERVE_SYNC_SECONDS", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SECONDS
    return value if value > 0 else DEFAULT_SECONDS


def sync_once() -> dict:
    """One import pass. Same call the CLI makes with --import."""
    return observe.scan(dry_run=False)


def _loop(log) -> None:
    while True:
        try:
            result = sync_once()
            inserted = int(result.get("inserted") or 0)
            if inserted:
                log(
                    f"observe sync: imported {inserted} row(s) from "
                    f"{result.get('files', '?')} session file(s)"
                )
        except Exception as exc:  # noqa: BLE001 - a bad sweep must not kill the daemon
            log(f"observe sync failed: {type(exc).__name__}: {exc}")
        time.sleep(interval())


def start(log=lambda _msg: None) -> threading.Thread | None:
    """Start the sweep thread; returns None when it is switched off."""
    if not enabled():
        log("observe sync disabled (NODUS_OBSERVE_SYNC=0)")
        return None
    log(f"observe sync: every {interval():.0f}s")
    thread = threading.Thread(
        target=_loop, args=(log,), name="observe-sync", daemon=True
    )
    thread.start()
    return thread
