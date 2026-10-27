r"""Magpie's own request log, read-only, as a coverage probe.

Magpie (A:\Tools\magpie) is the local model router in front of the agents: it
owns provider keys, account sign-ins and routing groups, and every request that
goes through its gateway is written to ~/.config/magpie/usage.jsonl with the
token counts the upstream reported (in/out/cache read/cache write), the agent,
provider, host, model asked for, model actually served, status and latency.

That makes it the one place on this machine that sees usage from every harness
at once — including ones Nodus cannot dispatch (Kimi, WorkBuddy, VS Code) — and
unlike CC Switch's estimates the numbers are per request, not a day x model
rollup. It still carries no money: this module reports tokens and requests,
never a cost, and writes nothing.

Path: ~/.config/magpie/usage.jsonl, override with NODUS_MAGPIE_USAGE.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_LOG = Path.home() / ".config" / "magpie" / "usage.jsonl"


def log_path() -> Path:
    return Path(os.environ.get("NODUS_MAGPIE_USAGE") or DEFAULT_LOG)


def _empty() -> dict:
    return {
        "requests": 0,
        "failed": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "ms": 0,
    }


def _add(bucket: dict, row: dict) -> None:
    bucket["requests"] += 1
    if int(row.get("status") or 0) >= 400:
        bucket["failed"] += 1
    tokens = bucket["tokens"]
    tokens["input"] += int(row.get("in") or 0)
    tokens["output"] += int(row.get("out") or 0)
    tokens["cache_read"] += int(row.get("cache_read") or 0)
    tokens["cache_write"] += int(row.get("cache_write") or 0)
    bucket["ms"] += int(row.get("ms") or 0)


def scan(file: Path | None = None) -> dict:
    path = Path(file) if file is not None else log_path()
    result: dict = {
        "path": str(path),
        "available": False,
        "notes": [
            "magpie 只记录经过它网关的请求；没走网关的调用不在里面",
            "这份日志没有金额，只有 token 和请求结果，不能当账本",
            "只读探针：不写 observation，不导入任何行",
        ],
    }
    if not path.is_file():
        result["error"] = f"no magpie usage log at {path}"
        return result
    rows = []
    bad = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    except OSError as exc:
        result["error"] = f"cannot read {path}: {exc}"
        return result
    total = _empty()
    by_agent: dict[str, dict] = {}
    by_provider: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    stamps = []
    for row in rows:
        _add(total, row)
        for key, bucket in (
            ("agent", by_agent),
            ("provider", by_provider),
            ("model", by_model),
        ):
            name = str(row.get(key) or "?")
            _add(bucket.setdefault(name, _empty()), row)
        if row.get("t"):
            stamps.append(str(row["t"]))
    result.update(total)
    result["rows"] = len(rows)
    result["unparsed"] = bad
    result["by_agent"] = by_agent
    result["by_provider"] = by_provider
    result["by_model"] = by_model
    result["start"] = stamps[0][:16] if stamps else ""
    result["end"] = stamps[-1][:16] if stamps else ""
    result["available"] = bool(rows)
    if not rows:
        result["error"] = "usage log is empty"
    return result
