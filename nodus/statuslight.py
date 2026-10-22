"""Turn the board's numbers into one sentence the pet can say.

The pet is a light on the desk, not a notification centre: it should speak only
when the state *changed for the worse* — a new thing waiting on you, a new
failure. Saying "3 waiting" every two minutes is how a pet becomes noise and
gets turned off, which is the failure mode the reference projects all hit.

Pure decisions live here (`line`, `calm`) so they can be tested without a
database or a desktop pet; only `snapshot` touches sqlite.
"""

from __future__ import annotations

import time

from . import db, pet

#: How often the daemon looks. Cheap (a few sqlite reads), so it can be short;
#: the pet still only speaks when a count goes *up*.
INTERVAL = 120.0


def snapshot() -> dict:
    """Current counts the pet cares about."""
    conn = db.connect()
    try:
        waiting = int(
            (conn.execute("SELECT COUNT(*) FROM proposal WHERE status = 'open'").fetchone() or [0])[0]
        )
    except Exception:  # a fresh database may not have the table yet
        waiting = 0
    finally:
        conn.close()

    live = db.live_runs()
    runs = db.list_runs(limit=50)
    failed = sum(1 for r in runs if r.get("status") == "failed")
    return {"waiting": waiting, "running": len(live), "failed": failed}


def line(prev: dict, cur: dict) -> tuple[str, str]:
    """What to say (and in which mood), or ("", "") when nothing got worse.

    Only increases speak. A count going down is good news and does not deserve
    a bubble; a count staying put is not news at all.
    """
    if cur["waiting"] > prev["waiting"]:
        return f"有 {cur['waiting']} 件等你决定", "waiting"
    if cur["failed"] > prev["failed"]:
        return f"{cur['failed']} 条跑红了", "error"
    return "", ""


def _loop(interval: float = INTERVAL) -> None:
    previous = snapshot()
    while True:
        time.sleep(interval)
        try:
            current = snapshot()
        except Exception:
            continue
        text, mood = line(previous, current)
        if text:
            pet.narrate(text, mood, "dsh")
        previous = current


def start(interval: float = INTERVAL):
    """Run the loop in the background; returns the thread (daemon)."""
    import threading

    thread = threading.Thread(target=_loop, args=(interval,), daemon=True)
    thread.start()
    return thread
