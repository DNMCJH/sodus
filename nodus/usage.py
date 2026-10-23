"""Usage and cost statistics: tokens, cache hit rate, money, per dimension.

One definition, stated here and repeated in the JSON so a consumer never has
to guess::

    cache_hit_rate = cache_read / (prompt + cache_read + cache_write)

The denominator is every prompt-side token: fresh input, cache reads and cache
writes. It is ``None`` when the denominator is zero -- not ``0``. A group where
nothing was recorded has an unknown rate, not a 0% rate.

The numbers are summed before the division. Averaging per-run rates would let a
ten-token run weigh as much as a ten-million-token one.

Money is never converted: ``cost`` is keyed by the currency recorded on the
run, and a run whose currency was not recorded goes under ``"-"`` rather than
being guessed to be USD. A run with no ``usage_json`` (or an empty one) is
``unrecorded``: it counts as a run, but it contributes no tokens, no money and
no place in any mean. It is *not* a zero-cost run.

Windows follow the operator's local calendar, matching ``server._day_start``:
a day is [local midnight, next local midnight), a month is [the 1st, next
month's 1st), so "today" does not move under them at 08:00 local time.
"""

from __future__ import annotations

import json
import time
from typing import Any, Mapping

from . import config, db

PERIODS = ("day", "month", "all")
GROUP_BYS = ("route", "model", "provider", "agent")

#: Cost rows with no recorded currency are kept apart under this key.
UNKNOWN_CURRENCY = "-"

CACHE_HIT_RATE_FORMULA = "cache_read / (prompt + cache_read + cache_write)"

_TOKEN_FIELDS = (
    ("input_tokens", "prompt"),
    ("output_tokens", "completion"),
    ("cache_read", "cache_read"),
    ("cache_write", "cache_write"),
)


def _local_midnight(year: int, month: int, day: int) -> float:
    """Midnight in the operator's timezone; ``mktime`` normalises overflows."""
    return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))


def bounds(period: str = "day", now: float | None = None) -> tuple[float | None, float | None, str]:
    """(start, end, label) for a period, in local time. ``end`` is exclusive.

    ``all`` has no bounds and includes runs that predate a usable timestamp.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}")
    moment = time.time() if now is None else float(now)
    lt = time.localtime(moment)
    if period == "all":
        return None, None, "全部历史"
    if period == "day":
        start = _local_midnight(lt.tm_year, lt.tm_mon, lt.tm_mday)
        end = _local_midnight(lt.tm_year, lt.tm_mon, lt.tm_mday + 1)
        return start, end, time.strftime("%Y-%m-%d", lt)
    start = _local_midnight(lt.tm_year, lt.tm_mon, 1)
    end = _local_midnight(lt.tm_year, lt.tm_mon + 1, 1)
    return start, end, time.strftime("%Y-%m", lt)


def _number(value: Any) -> bool:
    """A real number, not a bool dressed as one."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _usage_of(run: Mapping[str, Any]) -> dict:
    raw = run.get("usage_json")
    if isinstance(raw, dict):
        return raw
    if not raw or not isinstance(raw, str):
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _group_key(run: Mapping[str, Any], group_by: str) -> str:
    """Which bucket this run belongs to.

    Route falls back to the agent for rows written before the column existed;
    an unknown model/provider is named ``"-"`` instead of being folded into a
    real one.
    """
    if group_by == "route":
        return str(run.get("route") or run.get("agent") or "-")
    if group_by == "agent":
        return str(run.get("agent") or "-")
    return str(run.get(group_by) or "-")


def _blank() -> dict:
    return {
        "runs": 0,
        "completed": 0,
        "unrecorded": 0,
        "priced": 0,
        "tokens": {"prompt": 0, "completion": 0, "cache_read": 0, "cache_write": 0, "total": 0},
        "cost": {},
        "basis": {},
    }


def _add(bucket: dict, run: Mapping[str, Any]) -> None:
    bucket["runs"] += 1
    if run.get("status") == "completed":
        bucket["completed"] += 1
    usage = _usage_of(run)
    if not usage:
        bucket["unrecorded"] += 1
        return
    tokens = bucket["tokens"]
    for field, key in _TOKEN_FIELDS:
        value = usage.get(field)
        if _number(value):
            tokens[key] += int(value)
    cost = usage.get("cost")
    if not _number(cost):
        return
    currency = str(usage.get("currency") or UNKNOWN_CURRENCY)
    money = bucket["cost"].setdefault(currency, {"total": 0.0, "runs": 0})
    money["total"] += float(cost)
    money["runs"] += 1
    bucket["priced"] += 1
    basis = str(usage.get("cost_basis") or "none")
    bucket["basis"][basis] = bucket["basis"].get(basis, 0) + 1


def _finish(bucket: dict) -> dict:
    tokens = bucket["tokens"]
    tokens["total"] = (
        tokens["prompt"] + tokens["completion"] + tokens["cache_read"] + tokens["cache_write"]
    )
    denominator = tokens["prompt"] + tokens["cache_read"] + tokens["cache_write"]
    cost = {}
    for currency, money in sorted(bucket["cost"].items()):
        runs = money["runs"]
        cost[currency] = {
            "total": round(money["total"], 6),
            # Mean over runs that recorded a price in this currency only: an
            # unpriced run must not drag the average toward zero.
            "mean": round(money["total"] / runs, 6) if runs else None,
            "runs": runs,
        }
    return {
        "runs": bucket["runs"],
        "completed": bucket["completed"],
        "unrecorded": bucket["unrecorded"],
        "priced": bucket["priced"],
        "tokens": tokens,
        "cost": cost,
        "basis": dict(sorted(bucket["basis"].items())),
        "cache_hit_rate": round(tokens["cache_read"] / denominator, 4) if denominator else None,
    }


def summarize(
    runs: list[Mapping[str, Any]],
    group_by: str = "route",
    start: float | None = None,
    end: float | None = None,
) -> dict:
    """Aggregate run rows that the caller already selected.

    The server reuses this with rows read from its own connection; ``summary``
    is the DB-backed entry point. Groups are ordered by run count, then tokens.
    """
    if group_by not in GROUP_BYS:
        raise ValueError(f"group_by must be one of {', '.join(GROUP_BYS)}")
    buckets: dict[str, dict] = {}
    total = _blank()
    for run in runs:
        if start is not None or end is not None:
            started = run.get("started_at")
            if not _number(started):
                continue
            if start is not None and started < start:
                continue
            if end is not None and started >= end:
                continue
        _add(buckets.setdefault(_group_key(run, group_by), _blank()), run)
        _add(total, run)
    groups = [{"key": key, **_finish(bucket)} for key, bucket in buckets.items()]
    groups.sort(key=lambda group: (-group["runs"], -group["tokens"]["total"], group["key"]))
    return {"groups": groups, "totals": _finish(total)}


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
