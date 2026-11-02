"""Turn the board's numbers into one sentence the pet can say.

The pet is a light on the desk, not a notification centre: it should speak only
when the state *changed for the worse* — a new thing waiting on you, a new
failure. Saying "3 waiting" every two minutes is how a pet becomes noise and
gets turned off, which is the failure mode the reference projects all hit.

The numbers are not counted here. `snapshot` reads `server.board_state()` via
`server.attention()`: one failure counter, one window, the same number the page
quotes. The speaking rule (`line`) stays pure so it can be tested without a
database or a desktop pet.
"""

from __future__ import annotations

import time

from . import pet, server

#: How often the daemon looks. One board refresh per interval, so it stays in
#: step with the page; the pet still only speaks when a count goes *up*.
INTERVAL = 120.0


def snapshot() -> dict:
    """Current counts the pet cares about, taken from the board.

    Scope is `server.attention`'s scope, deliberately: `waiting` is the board's
    `needs_you` (proposals + spend gates), `failed` is the board's failed count
    over the 50 newest work runs (process failed or completed with red tests),
    and `running` is the board's live list. A chat row cannot inflate `failed`,
    and an open spend decision cannot be invisible to `waiting`.
    """
    return server.attention()


def line(prev: dict, cur: dict) -> tuple[str, str]:
    """What to say (and in which mood), or ("", "") when nothing got worse.

    Only increases speak. A count going down is good news and does not deserve
    a bubble; a count staying put is not news at all. `prev` and `cur` are
    `snapshot()` dicts, so rows moving between board sections do not make the
    pet repeat itself as long as the board's numbers stay the same.
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
