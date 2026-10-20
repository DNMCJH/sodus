"""Billing meters.

A vendor's own meter is the only cost number that is not somebody's estimate.
Two shapes exist:

    usage    cumulative amount charged so far    (relay: /v1/dashboard/billing/usage)
    balance  prepaid balance remaining           (DeepSeek: /user/balance)

We read the meter immediately before and after a run and record the delta.
A CLI's local price book is a guess about somebody else's bill: Claude Code
prices whatever backend it reaches with Anthropic's rates, and a relay adds its
own multiplier on top. Reading the meter skips both problems.

Meters fail open: a run must never be lost because billing was unreachable.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .agents import AgentSpec

DEEPSEEK_BALANCE = "https://api.deepseek.com/user/balance"


@dataclass(frozen=True)
class Reading:
    value: float
    unit: str  # "relay_unit" | "cny"
    kind: str  # "usage" | "balance"


def _get_json(url: str, token: str, timeout: float = 15.0) -> Any:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "nodus/0.2"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _agent_env(spec: "AgentSpec") -> dict:
    """The env block of an agent's isolated CLI config, if it has one."""
    home = getattr(spec, "config_home", None)
    if home is None:
        return {}
    path = home / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("env", {}) or {}
    except (json.JSONDecodeError, OSError):
        return {}


def _relay(spec: "AgentSpec") -> Reading | None:
    env = _agent_env(spec)
    base = (env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = env.get("ANTHROPIC_AUTH_TOKEN") or ""
    if not base.startswith("http") or not token:
        return None
    try:
        body = _get_json(f"{base}/v1/dashboard/billing/usage", token)
        # OpenAI-compatible: total_usage is in cents of the site's own unit.
        return Reading(value=float(body["total_usage"]) / 100.0, unit="relay_unit", kind="usage")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError):
        return None


def _deepseek(spec: "AgentSpec") -> Reading | None:
    token = config.load_secrets().get("DEEPSEEK_API_KEY") or ""
    if not token:
        return None
    try:
        body = _get_json(DEEPSEEK_BALANCE, token)
        info = (body.get("balance_infos") or [{}])[0]
        return Reading(value=float(info["total_balance"]), unit="cny", kind="balance")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError):
        return None


READERS = {"relay": _relay, "deepseek": _deepseek}


def read(spec: "AgentSpec", settle_from: Reading | None = None, settle: float = 4.0) -> Reading | None:
    """Read the spec's meter. With `settle_from`, wait briefly for billing to land."""
    reader = READERS.get(getattr(spec, "meter", None) or "")
    if reader is None:
        return None
    deadline = time.time() + (settle if settle_from is not None else 0)
    first = reader(spec)
    if first is None:
        return None
    if settle_from is None:
        return first
    while time.time() < deadline and first.value <= settle_from.value:
        time.sleep(0.8)
        again = reader(spec)
        if again is None:
            return first
        first = again
    return first


def delta(before: Reading | None, after: Reading | None) -> float | None:
    """What this run cost, in the meter's own unit. None when unmeasurable."""
    if before is None or after is None or before.kind != after.kind:
        return None
    raw = after.value - before.value if before.kind == "usage" else before.value - after.value
    if raw < 0:  # balance topped up mid-run, or a meter glitch
        return None
    return raw
