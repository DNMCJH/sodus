"""A spend ceiling for the whole machine, not just one task.

`runner` already refuses a run that would blow its own task budget. Nothing
stopped a dozen tasks from each staying inside their own budget and adding up
to a number nobody agreed to. That is the failure mode a daily ceiling fixes.

Policy, deliberately small:

- the limit lives in config (`budget.daily_cny`, or `NODUS_DAILY_BUDGET`)
- only CNY-denominated costs count; anything the gateway reports in USD is
  excluded rather than converted at an invented rate
- reaching the limit refuses *new* runs; it never kills a running one — a run
  that is already editing a tree is worth more than the last few fen
- the board always shows the number, so a refusal is never a surprise
"""

from __future__ import annotations

import os
import time

from . import config, db, ledger

DEFAULT_KEY = "budget"
#: Currency this ceiling is expressed in. Other currencies are not converted.
CURRENCY = "CNY"


def limit() -> float:
    """The daily ceiling in CNY, or 0.0 when no ceiling is configured."""
    env = os.environ.get("NODUS_DAILY_BUDGET")
    if env:
        try:
            return max(0.0, float(env))
        except ValueError:
            return 0.0
    raw = config.load_config().get(DEFAULT_KEY) or {}
    if isinstance(raw, dict):
        try:
            return max(0.0, float(raw.get("daily_cny") or 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def day_start(now: float | None = None) -> float:
    """Local midnight for `now` (the same boundary the board's money uses)."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))


def spent(now: float | None = None) -> float:
    """CNY spent today, straight from what `_price()` wrote. No re-derivation.

    The reading is `ledger.spent`, with this module's long-standing rule passed
    in rather than assumed: a row that recorded no currency counts as CNY
    here. Every row this workspace writes is CNY, and a ceiling that silently
    ignores unlabelled spend would let a run through the gate it is meant to
    close. The board tells a different truth -- an unnamed amount is not USD --
    and the two are not the same question.
    """
    start = day_start(now)
    rows = db.runs_in_window(start)
    return ledger.spent(rows, CURRENCY, unrecorded_is=CURRENCY)


def check(now: float | None = None) -> dict:
    """Can another run start right now, and what do the numbers look like?"""
    cap = limit()
    used = spent(now)
    over = bool(cap) and used >= cap
    return {
        "limit": cap,
        "spent": round(used, 6),
        "over": over,
        "remaining": round(max(0.0, cap - used), 6) if cap else None,
    }


def refuse_message(info: dict) -> str:
    return (
        f"today's spend already reached the daily ceiling "
        f"({info['spent']:.2f} / {info['limit']:.2f} {CURRENCY}); raise budget.daily_cny "
        f"or NODUS_DAILY_BUDGET to continue"
    )
