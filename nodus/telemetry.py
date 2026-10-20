"""Usage accounting and provider liveness probes.

Usage comes from the run table (already collected per run). Probes use each
provider's model-list endpoint, which is free and also tells us whether the key
still works. No probe spends generation tokens.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import config, db

PROBE_LOG = "probes.jsonl"


def _claude_env() -> dict:
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("env", {})
    except json.JSONDecodeError:
        return {}


def providers() -> list[dict]:
    secrets = config.load_secrets()
    claude = _claude_env()
    out: list[dict] = []
    base = (claude.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = claude.get("ANTHROPIC_AUTH_TOKEN") or ""
    if base and token:
        out.append(
            {
                "name": "9527",
                "url": f"{base}/v1/models",
                "headers": {"Authorization": f"Bearer {token}"},
                "proxy": None,
            }
        )
    ts_key = secrets.get("TYPESAFE_API_KEY") or ""
    if ts_key:
        out.append(
            {
                "name": "typesafe",
                "url": "https://api.typesafe.ai/v1/models",
                "headers": {"Authorization": f"Bearer {ts_key}"},
                "proxy": secrets.get("TYPESAFE_PROXY") or "http://127.0.0.1:7890",
            }
        )
    ds_key = secrets.get("DEEPSEEK_API_KEY") or ""
    if ds_key:
        out.append(
            {
                "name": "deepseek",
                "url": "https://api.deepseek.com/models",
                "headers": {"Authorization": f"Bearer {ds_key}"},
                "proxy": None,
            }
        )
    return out


def _opener(proxy: str | None):
    if proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def probe() -> list[dict]:
    results = []
    for p in providers():
        started = time.time()
        record = {"ts": started, "provider": p["name"], "url": p["url"]}
        try:
            headers = {**p["headers"], "User-Agent": "nodus/0.3"}
            req = urllib.request.Request(p["url"], headers=headers)
            with _opener(p.get("proxy")).open(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            models = body.get("data") or body.get("models") or []
            record.update(
                {
                    "ok": True,
                    "status": resp.status,
                    "models": len(models),
                    "latency_ms": int((time.time() - started) * 1000),
                }
            )
        except urllib.error.HTTPError as exc:
            record.update({"ok": False, "status": exc.code, "error": exc.reason})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            record.update({"ok": False, "status": 0, "error": str(exc)})
        results.append(record)
        config.ensure_dirs()
        with (config.HOME / PROBE_LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return results


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
    """Prepaid balance for relays that expose OpenAI-style billing endpoints."""
    claude = _claude_env()
    base = (claude.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
    token = claude.get("ANTHROPIC_AUTH_TOKEN") or ""
    if not base or not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "User-Agent": "nodus/0.3"}
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
        "provider": "9527",
        "limit_usd": limit,
        "used_usd": round(used, 2),
        "remaining_usd": round(limit - used, 2),
    }


def usage_summary(days: float = 7.0) -> dict:
    cutoff = time.time() - days * 86400
    totals: dict[str, dict] = {}
    for run in db.list_runs(limit=10000):
        if (run.get("started_at") or 0) < cutoff:
            continue
        try:
            usage = json.loads(run.get("usage_json") or "{}")
        except json.JSONDecodeError:
            usage = {}
        bucket = totals.setdefault(
            run["agent"],
            {"runs": 0, "in": 0, "out": 0, "cache_read": 0, "failed": 0, "costs": {}, "basis": set()},
        )
        bucket["runs"] += 1
        bucket["in"] += int(usage.get("input_tokens") or 0)
        bucket["out"] += int(usage.get("output_tokens") or 0)
        bucket["cache_read"] += int(usage.get("cache_read") or 0)
        if isinstance(usage.get("cost"), (int, float)):
            currency = usage.get("currency") or "USD"
            bucket["costs"][currency] = bucket["costs"].get(currency, 0.0) + float(usage["cost"])
            bucket["basis"].add(usage.get("cost_basis") or "reported")
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
