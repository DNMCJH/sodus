"""A spend ceiling for the whole machine, not just one task.

`runner` already refuses a run that would blow its own task budget. Nothing
stopped a dozen tasks from each staying inside their own budget and adding up
to a number nobody agreed to. That is the failure mode a daily ceiling fixes.

Policy, deliberately small:

- the limit lives in config (`budget.daily_cny`, or `SODUS_DAILY_BUDGET`)
- only CNY-denominated costs count; anything the gateway reports in USD is
  excluded rather than converted at an invented rate
- reaching the limit refuses *new* runs; it never kills a running one — a run
  that is already editing a tree is worth more than the last few fen
- the board always shows the number, so a refusal is never a surprise
"""

from __future__ import annotations

import os
import json
import time

from . import config, db, ledger

DEFAULT_KEY = "budget"
#: Currency this ceiling is expressed in. Other currencies are not converted.
CURRENCY = "CNY"


def _get_phase2_limits() -> tuple[float | None, float | None]:
    """Return (phase2_total_cny, phase2_per_run_max_cny) from config, or (None, None)."""
    cfg = config.load_config()
    budget_cfg = cfg.get("budget", {})
    return (
        budget_cfg.get("phase2_cny"),
        budget_cfg.get("phase2_per_run_max_cny")
    )


def check_phase2(task_id: str = "", now: float | None = None) -> dict | None:
    """Check Phase 2 budget limits if configured. Returns error dict or None if OK.

    Phase 2 limits are opt-in caps for a specific development phase:
    - phase2_cny: total CNY spend allowed in Phase 2 (cumulative across all runs)
    - phase2_per_run_max_cny: max CNY per single run

    Returns None if limits are not configured or not exceeded.
    Returns error dict {"over": True, "message": str} if limit is exceeded.
    """
    total_limit, per_run_limit = _get_phase2_limits()
    if not total_limit and not per_run_limit:
        return None  # No Phase 2 limits configured

    used = spent(now)

    # Check total Phase 2 budget
    if total_limit and used >= total_limit:
        return {
            "over": True,
            "message": (
                f"Phase 2 budget exhausted: {used:.2f} / {total_limit:.2f} {CURRENCY} spent. "
                f"Raise budget.phase2_cny in ~/.sodus/config.json to continue."
            )
        }

    # Check per-run budget (estimate based on past runs for this task's route/model)
    # For now, we only enforce this as a warning since we don't know run cost upfront
    if per_run_limit:
        # TODO: Could check recent run costs for this route/model and warn if average > limit
        pass

    return None


def limit() -> float:
    """The daily ceiling in CNY, or 0.0 when no ceiling is configured."""
    env = os.environ.get("SODUS_DAILY_BUDGET")
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
            result = tuple(str(name).strip() for name in names if str(name).strip())
            if result:
                return result
    # Default: opus and fable are expensive (high-tier reasoning models)
    return ("claude-opus", "claude-fable")


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


def model_family(model: str = "", route: str = "", usage_json=None, agent: str = "") -> str:
    """Classify recorded model first, then the first attempt, then route/agent."""
    if not model and usage_json:
        try:
            usage = json.loads(usage_json) if isinstance(usage_json, str) else usage_json
            attempts = usage.get("attempts") or []
            model = attempts[0].get("model") or "" if attempts else ""
        except (ValueError, TypeError, AttributeError, IndexError):
            model = ""
    name = model.lower()
    if name:
        if any(x in name for x in ("claude", "opus", "sonnet", "haiku")):
            return "claude"
        if name.startswith("gpt") or any(x in name for x in ("gpt-", "o1", "o3", "o4", "o5", "codex")):
            return "gpt"
        return "other"
    name = (route or agent).lower()
    if any(x in name for x in ("opus", "claude", "sonnet")):
        return "claude"
    if any(x in name for x in ("sol", "codex", "terra")):
        return "gpt"
    return "other"


def family_limits() -> dict[str, float]:
    """Configured CNY soft ceilings; absent and zero values disable a family."""
    raw = config.load_config().get(DEFAULT_KEY) or {}
    raw = raw.get("soft_families", {}) if isinstance(raw, dict) else {}
    result = {}
    for family in ("claude", "gpt", "other"):
        try:
            result[family] = max(0.0, float(raw.get(family) or 0)) if isinstance(raw, dict) else 0.0
        except (TypeError, ValueError):
            result[family] = 0.0
    return result


def spent_by_family(now: float | None = None) -> dict[str, float]:
    totals = dict.fromkeys(("claude", "gpt", "other"), 0.0)
    for row in db.runs_in_window(day_start(now)):
        family = model_family(row.get("model") or "", row.get("route") or "",
                              row.get("usage_json"), row.get("agent") or "")
        totals[family] += ledger.spent([row], CURRENCY, unrecorded_is=CURRENCY)
    return totals


def check(
    now: float | None = None,
    route: str = "",
    model: str = "",
    allow_expensive: bool = False,
    agent: str = "",
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
    family = model_family(model, route, agent=agent)
    family_limit = family_limits()[family]
    family_spent = spent_by_family(now)[family]
    family_gated = bool(family_limit) and family_spent >= family_limit and not allow_expensive
    gated = expensive and bool(soft) and used >= soft and not allow_expensive
    return {
        "limit": cap,
        "soft_limit": soft,
        "spent": round(used, 6),
        "over": over,
        "remaining": round(max(0.0, cap - used), 6) if cap else None,
        "expensive": expensive,
        "gated": gated or family_gated,
        "family": family,
        "family_limit": family_limit,
        "family_spent": round(family_spent, 6),
        "family_gated": family_gated,
        "agent": agent,
        "match": match,
        "route": route,
        "model": model,
    }


def blocked_message(info: dict) -> str:
    """Why the run was refused, in the terms the operator set."""
    if info.get("over"):
        return refuse_message(info)
    if info.get("family_gated"):
        return (f"{info['family']} family spend reached its soft limit "
                f"({info['family_spent']:.2f} / {info['family_limit']:.2f} {CURRENCY}); "
                "pass --allow-expensive for a deliberate one-off")
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
        f"or SODUS_DAILY_BUDGET to continue"
    )
