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


def soft_limit() -> float:
    """The spend at which expensive routes stop being allowed (0.0 = off).

    A single ceiling is too blunt for a machine that runs cheap models all day
    and an expensive one once a week: either the cap blocks the cheap work, or
    it lets one opus run eat the whole day. The soft limit is the middle line —
    cheap routes keep running up to the hard ceiling, expensive ones need an
    explicit opt-in after this much is already spent.
    """
    raw = config.load_config().get(DEFAULT_KEY) or {}
    if isinstance(raw, dict):
        try:
            return max(0.0, float(raw.get("soft_cny") or 0.0))
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def expensive_routes() -> tuple[str, ...]:
    """Route names that only run past the soft limit with an explicit opt-in."""
    raw = config.load_config().get(DEFAULT_KEY) or {}
    if isinstance(raw, dict):
        names = raw.get("expensive_routes") or ()
        if isinstance(names, (list, tuple)):
            return tuple(str(name).strip() for name in names if str(name).strip())
    return ()


def expensive_models() -> tuple[str, ...]:
    """Model-name prefixes that count as expensive wherever they are pinned.

    A route name is not enough: `claude` can run sonnet or opus depending on
    what the CLI defaults to, and `--model` can pin an expensive model on a
    cheap route. Prefix matching keeps the config short ("claude-opus") while
    still catching the versioned names the vendors record.
    """
    raw = config.load_config().get(DEFAULT_KEY) or {}
    if isinstance(raw, dict):
        names = raw.get("expensive_models") or ()
        if isinstance(names, (list, tuple)):
            return tuple(str(name).strip() for name in names if str(name).strip())
    return ()


def model_is_expensive(model: str) -> bool:
    if not model:
        return False
    return any(model.startswith(prefix) for prefix in expensive_models())


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


def check(
    now: float | None = None,
    route: str = "",
    model: str = "",
    allow_expensive: bool = False,
) -> dict:
    """Can another run start right now, and what do the numbers look like?"""
    cap = limit()
    soft = soft_limit()
    used = spent(now)
    over = bool(cap) and used >= cap
    route_expensive = bool(route) and route in expensive_routes()
    model_expensive = model_is_expensive(model)
    expensive = route_expensive or model_expensive
    match = "model" if model_expensive else ("route" if route_expensive else "")
    gated = expensive and bool(soft) and used >= soft and not allow_expensive
    return {
        "limit": cap,
        "soft_limit": soft,
        "spent": round(used, 6),
        "over": over,
        "remaining": round(max(0.0, cap - used), 6) if cap else None,
        "expensive": expensive,
        "gated": gated,
        "match": match,
        "route": route,
        "model": model,
    }


def blocked_message(info: dict) -> str:
    """Why the run was refused, in the terms the operator set."""
    if info.get("over"):
        return refuse_message(info)
    if info.get("match") == "model":
        subject = info.get("model") or "this model"
    else:
        subject = info.get("route") or info.get("model") or "this run"
    return (
        f"{subject} is expensive and today's spend is already "
        f"{info['spent']:.2f} {CURRENCY} (soft limit {info['soft_limit']:.2f}); "
        f"pass --allow-expensive for a deliberate one-off, or use a cheaper route"
    )


def refuse_message(info: dict) -> str:
    return (
        f"today's spend already reached the daily ceiling "
        f"({info['spent']:.2f} / {info['limit']:.2f} {CURRENCY}); raise budget.daily_cny "
        f"or NODUS_DAILY_BUDGET to continue"
    )
