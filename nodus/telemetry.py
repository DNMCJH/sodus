"""Usage accounting and endpoint liveness probes.

Usage comes from the run table (already collected per attempt). Probes ask each
provider for its model list, which is free and also tells us whether the key
still works. No probe spends generation tokens.

Reading `usage_json` is `ledger`'s job, not this module's: token fields, the
price, and the currency bucket all come from `ledger.parse` /
`ledger.currency_of`. That is what makes a run with no recorded currency land
under `"-"` here exactly as it does on the board and in `nodus stats`, instead
of being quietly relabelled `USD` (the old 7x error).

Endpoints come from the provider registry, so this module has no list of its
own to drift out of date.
"""

from __future__ import annotations

import json
import time

from . import config, db, ledger, providers

PROBE_LOG = "probes.jsonl"


def record(result: dict) -> dict:
    """Append one probe result to the log `nodus status` reads.

    Both probe commands go through here; before, `providers probe` skipped the
    log and `status` kept saying "never probed" right after a successful probe.
    """
    result.setdefault("ts", time.time())
    config.ensure_dirs()
    with (config.HOME / PROBE_LOG).open("a", encoding="utf-8") as f:
        f.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def probe() -> list[dict]:
    secrets = config.load_secrets()
    return [record(providers.probe(p, secrets)) for p in sorted(
        providers.load().values(), key=lambda p: p.name
    )]


def last_probes() -> dict[str, dict]:
    path = config.HOME / PROBE_LOG
    if not path.exists():
        return {}
    latest: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        latest[r["provider"]] = r
    return latest


def balance() -> dict | None:
    """Prepaid balance for a relay that exposes OpenAI-style billing endpoints."""
    secrets = config.load_secrets()
    provider = providers.get("relay")
    if provider is None:
        return None
    base = (provider.base_url or "").rstrip("/")
    token = providers.secret_value(provider, secrets)
    if not base or not token:
        return None
    import urllib.error
    import urllib.request

    headers = {"Authorization": f"Bearer {token}", "User-Agent": "nodus/0.4"}
    try:
        sub = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(f"{base}/v1/dashboard/billing/subscription", headers=headers),
                timeout=15,
            ).read().decode("utf-8")
        )
        usage = json.loads(
            urllib.request.urlopen(
                urllib.request.Request(f"{base}/v1/dashboard/billing/usage", headers=headers),
                timeout=15,
            ).read().decode("utf-8")
        )
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError):
        return None
    limit = float(sub.get("hard_limit_usd") or 0)
    used = float(usage.get("total_usage") or 0) / 100.0  # OpenAI returns cents
    return {
        "provider": "relay",
        "limit_usd": limit,
        "used_usd": round(used, 2),
        "remaining_usd": round(limit - used, 2),
    }


def usage_summary(days: float = 7.0) -> dict:
    """Per-route totals.

    Routed, not per-agent: "claude" no longer names one endpoint, and a report
    that merges a relay run with a MiniMax run hides the number that matters.
    Runs from before the split have no route column and fall back to the agent.

    Money is read by `ledger`: a run whose currency was not recorded goes under
    `"-"` (`ledger.UNKNOWN_CURRENCY`), never `USD`. The shape is the legacy one
    the CLI prints; the numbers are the ledger's.
    """
    cutoff = time.time() - days * 86400
    totals: dict[str, dict] = {}
    for run in db.list_runs(limit=10000):
        if (run.get("started_at") or 0) < cutoff:
            continue
        parsed = ledger.parse(run.get("usage_json"))
        tokens = parsed.tokens.as_report()
        key = run.get("route") or run["agent"]
        bucket = totals.setdefault(
            key,
            {
                "runs": 0,
                "in": 0,
                "out": 0,
                "cache_read": 0,
                "failed": 0,
                "costs": {},
                "basis": set(),
                "providers": set(),
                "fallbacks": 0,
            },
        )
        bucket["runs"] += 1
        bucket["in"] += tokens["prompt"]
        bucket["out"] += tokens["completion"]
        bucket["cache_read"] += tokens["cache_read"]
        bucket["providers"].add(run.get("provider") or "-")
        if len(parsed.cost.attempts) > 1:
            bucket["fallbacks"] += 1
        if parsed.cost.amount is not None:
            currency = ledger.currency_of(parsed)
            bucket["costs"][currency] = bucket["costs"].get(currency, 0.0) + float(
                parsed.cost.amount
            )
            bucket["basis"].add(parsed.cost.basis or "none")
        if run.get("status") == "failed":
            bucket["failed"] += 1
    return totals


def decision_usage(days: float = 1.0) -> dict:
    path = config.HOME / "decisions.jsonl"
    cutoff = time.time() - days * 86400
    total = {"calls": 0, "in": 0, "out": 0, "unavailable": 0}
    if not path.exists():
        return total
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("ts", 0) < cutoff:
            continue
        total["calls"] += 1
        if not r.get("ok"):
            total["unavailable"] += 1
        usage = r.get("usage") or {}
        total["in"] += int(usage.get("input_tokens") or 0)
        total["out"] += int(usage.get("output_tokens") or 0)
    return total
