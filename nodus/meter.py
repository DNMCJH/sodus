"""Billing meters.

A vendor's own meter is the only cost number that is not somebody's estimate.
Two shapes exist:

    usage    cumulative amount charged so far    (relay: /v1/dashboard/billing/usage)
    balance  prepaid balance remaining           (DeepSeek: /user/balance)

We read the meter immediately before and after a run and record the delta.
A CLI's local price book is a guess about somebody else's bill: Claude Code
prices whatever backend it reaches with Anthropic's rates, and a relay adds its
own multiplier on top. Reading the meter skips both problems.

A meter belongs to an endpoint, so this module takes a `Provider` and reads the
URL and key from it. It used to reverse-engineer them out of a Claude settings
file, which meant the same fact had two homes and could disagree with itself.

Meters fail open: a run must never be lost because billing was unreachable.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import config, providers

DEEPSEEK_BALANCE = "https://api.deepseek.com/user/balance"
GATEWAY_DEFAULT = "http://127.0.0.1:3425"


@dataclass(frozen=True)
class Reading:
    value: float
    unit: str  # "relay_unit" | "cny"
    kind: str  # "usage" | "balance"


def _get_json(url: str, token: str | None = None, timeout: float = 15.0):
    headers = {"User-Agent": "nodus/0.4"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _relay(provider: providers.Provider, secrets: dict[str, str]) -> Reading | None:
    # The billing endpoint hangs off the account root, not the protocol root:
    # the OpenAI face of the same relay has base_url ".../v1" and would
    # otherwise be asked for ".../v1/v1/dashboard/billing/usage".
    base = (provider.base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    token = providers.secret_value(provider, secrets)
    if not base.startswith("http") or not token:
        return None
    try:
        body = _get_json(f"{base}/v1/dashboard/billing/usage", token)
        # OpenAI-compatible: total_usage is in cents of the site's own unit.
        return Reading(value=float(body["total_usage"]) / 100.0, unit="relay_unit", kind="usage")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError):
        return None


def _deepseek(provider: providers.Provider, secrets: dict[str, str]) -> Reading | None:
    token = providers.secret_value(provider, secrets)
    if not token:
        return None
    try:
        body = _get_json(DEEPSEEK_BALANCE, token)
        info = (body.get("balance_infos") or [{}])[0]
        return Reading(value=float(info["total_balance"]), unit="cny", kind="balance")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError):
        return None


READERS = {"relay": _relay, "deepseek": _deepseek}


def read(
    provider: providers.Provider,
    secrets: dict[str, str],
    settle_from: Reading | None = None,
    settle: float = 4.0,
) -> Reading | None:
    """Read this endpoint's meter. With `settle_from`, wait for billing to land."""
    reader = READERS.get(provider.meter or "")
    if reader is None:
        return None
    deadline = time.time() + (settle if settle_from is not None else 0)
    first = reader(provider, secrets)
    if first is None:
        return None
    if settle_from is None:
        return first
    while time.time() < deadline and first.value <= settle_from.value:
        time.sleep(0.8)
        again = reader(provider, secrets)
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


def _gateway_base() -> str:
    raw = os.environ.get("NODUS_MAGPIE_URL") or config.load_config().get("magpie_url")
    return str(raw or GATEWAY_DEFAULT).rstrip("/")


def request_cost(
    session_id: str, base: str | None = None, settle: float = 0.0
) -> tuple[float, int] | None:
    """Exact cost of one agent session, read from a gateway's request log.

    A gateway that parses each agent's local session log knows the cost of
    every single call, keyed by the session it belongs to. That is strictly
    better than a balance delta: exact, and immune to whatever else was
    spending on the same key at the same time.

    `settle` retries while the gateway has not caught up yet — it tails session
    files, so the row can land seconds after the run exits.

    Returns (cost, requests) or None. Never raises — this is enrichment.
    """
    if not session_id:
        return None
    deadline = time.time() + max(0.0, settle)
    while True:
        result = _request_cost_once(session_id, base)
        if result is not None or time.time() >= deadline:
            return result
        time.sleep(1.5)


def _request_cost_once(session_id: str, base: str | None = None) -> tuple[float, int] | None:
    url = f"{base or _gateway_base()}/v1/magpie/usage/requests?period=7d&limit=5000"
    try:
        body = _get_json(url, timeout=8.0)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    rows = [
        row
        for row in body.get("rows") or []
        if row.get("session") == session_id and row.get("priced")
    ]
    if not rows:
        return None
    total = sum(float(row.get("cost") or 0) for row in rows)
    return round(total, 6), len(rows)
