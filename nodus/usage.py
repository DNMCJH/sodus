"""Usage and cost statistics: tokens, cache hit rate, money, per dimension.

The definitions themselves live in `ledger` now -- parsing `usage_json`,
deciding what an absent field means, adding runs up and keeping currencies
apart. This module is the command behind them: which rows to read, and the
JSON envelope `nodus stats usage --json` prints. Keeping it a thin layer is
the point; a second copy of the cache-hit formula here is how the CLI and the
board started disagreeing.

One definition, stated in `ledger` and repeated in the JSON so a consumer never
has to guess::

    cache_hit_rate = cache_read / (prompt + cache_read + cache_write)

The denominator is every prompt-side token: fresh input, cache reads and cache
writes. It is ``None`` when the denominator is zero -- not ``0``. A group where
nothing was recorded has an unknown rate, not a 0% rate.

Money is never converted: ``cost`` is keyed by the currency recorded on the
run, and a run whose currency was not recorded goes under ``"-"`` rather than
being guessed to be USD.

Windows follow the operator's local calendar: a day is [local midnight, next
local midnight), a month is [the 1st, next month's 1st), so "today" does not
move under them at 08:00 local time.
"""

from __future__ import annotations

from typing import Any

from . import config, db, ledger

PERIODS = ledger.PERIODS
GROUP_BYS = ledger.GROUP_BYS

#: Cost rows with no recorded currency are kept apart under this key.
UNKNOWN_CURRENCY = ledger.UNKNOWN_CURRENCY

CACHE_HIT_RATE_FORMULA = ledger.CACHE_HIT_RATE_FORMULA

# Re-exported: `usage.bounds` predates `ledger` and callers (server.board_state,
# the CLI's period choices) still ask for it by this name.
bounds = ledger.bounds
summarize = ledger.summarize


def _rows(start: float | None = None, end: float | None = None) -> list[dict]:
    """Every run in this workspace whose start falls inside the window.

    Deliberately not ``db.list_runs(limit=...)``: a stats command must not
    silently drop old rows because a page size was chosen for a different job.
    """
    sql = (
        "SELECT agent, route, provider, model, status, usage_json, started_at"
        " FROM run WHERE workspace_id=?"
    )
    args: list[Any] = [config.workspace_id()]
    if start is not None:
        sql += " AND started_at IS NOT NULL AND started_at>=?"
        args.append(start)
    if end is not None:
        sql += " AND started_at<?"
        args.append(end)
    conn = db.connect()
    try:
        return [dict(row) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def summary(period: str = "day", group_by: str = "route", now: float | None = None) -> dict:
    """DB-backed report: window + groups + totals, JSON-serialisable."""
    start, end, label = bounds(period, now)
    data = summarize(_rows(start, end), group_by=group_by, start=start, end=end)
    return {
        "period": period,
        "group_by": group_by,
        "window": {"start": start, "end": end, "label": label},
        "cache_hit_rate_formula": CACHE_HIT_RATE_FORMULA,
        "groups": data["groups"],
        "totals": data["totals"],
    }
