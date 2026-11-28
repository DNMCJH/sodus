"""Internal LLM client for sodus's own reasoning needs.

Calls a configured route (default: "coordinator") directly via the
Anthropic Messages API, without spawning a Claude Code subprocess.

Design rules (same as decision.py):
- Fail open: missing key, timeout, or any HTTP error returns
  {"ok": False, "unavailable": "..."}, caller continues with defaults.
- No side effects on the task graph: cortex calls are advisory. They
  never write runs, never touch the DB, never grant permissions.
- Every call is logged to ~/.sodus/cortex.jsonl for auditability.

Typical caller: clarify.py (L1 requirements intake).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import config, providers, routes

DEFAULT_ROUTE = "coordinator"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve(route_name: str) -> tuple["providers.Provider", str, str] | None:
    """Return (provider, model, key) for a route, or None if unavailable."""
    route = routes.resolve(route_name)
    if route is None:
        return None
    prov_table = providers.load()
    secrets = config.load_secrets()
    for pname in route.providers:
        prov = prov_table.get(pname)
        if prov is None or prov.protocol != providers.ANTHROPIC:
            continue
        # Reject empty base_url: would send relay keys to api.anthropic.com
        if not prov.base_url:
            continue
        key = providers.secret_value(prov, secrets)
        if not key:
            continue
        # Prefer route-level model, fall back to provider default
        model = route.model or prov.tiers.get("sonnet") or prov.model or ""
        if not model:
            continue
        return prov, model, key
    return None


def _log(record: dict) -> None:
    try:
        log_dir = config.HOME
        log_dir.mkdir(parents=True, exist_ok=True)
        with (log_dir / "cortex.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def ask(
    system: str,
    messages: list[dict[str, Any]],
    *,
    route: str = DEFAULT_ROUTE,
    max_tokens: int = 2048,
    timeout: float = 60.0,
) -> dict:
    """Send a synchronous messages-API request via the given route.

    Returns:
        {"ok": True, "text": str, "model": str, "latency_ms": int}
        {"ok": False, "unavailable": str}
    """
    resolved = _resolve(route)
    if resolved is None:
        return {"ok": False, "unavailable": f"route {route!r} not available or no key"}
    prov, model, key = resolved

    base = (prov.base_url or "https://api.anthropic.com").rstrip("/")
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": messages,
    }
    req = urllib.request.Request(
        f"{base}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "user-agent": f"sodus/{config.VERSION}/cortex",
        },
    )
    started = time.time()
    call_id = hashlib.sha1(
        f"{time.time()}{system[:40]}".encode()
    ).hexdigest()[:8]
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        result: dict = {"ok": False, "unavailable": f"HTTP {exc.code}", "detail": detail}
        _log({"id": call_id, "route": route, "model": model, **result,
              "latency_ms": int((time.time() - started) * 1000)})
        return result
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        result = {"ok": False, "unavailable": f"network: {exc}"}
        _log({"id": call_id, "route": route, "model": model, **result,
              "latency_ms": int((time.time() - started) * 1000)})
        return result

    latency_ms = int((time.time() - started) * 1000)
    # Decode and parse defensively — relay may return HTML on error
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        result = {"ok": False, "unavailable": f"JSON decode: {exc}",
                  "raw_prefix": raw[:120].decode("utf-8", "replace")}
        _log({"id": call_id, "route": route, "model": model, **result,
              "latency_ms": latency_ms})
        return result

    # body must be a dict with a "content" list
    if not isinstance(body, dict):
        result = {"ok": False, "unavailable": "unexpected response shape (not dict)"}
        _log({"id": call_id, "route": route, "model": model, **result,
              "latency_ms": latency_ms})
        return result

    content = body.get("content") or []
    text = "".join(
        block.get("text", "") for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    result = {"ok": True, "text": text, "model": model, "latency_ms": latency_ms}
    _log({
        "id": call_id,
        "route": route,
        "model": model,
        "ok": True,
        "latency_ms": latency_ms,
        "in_tokens": (body.get("usage") or {}).get("input_tokens", 0),
        "out_tokens": (body.get("usage") or {}).get("output_tokens", 0),
    })
    return result
