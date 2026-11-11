"""Read CC Switch's own usage tables, read-only, as a coverage probe.

CC Switch (github.com/farion1231/cc-switch) is a config switcher for a dozen
harnesses. It keeps two independent usage stores in its SQLite file:

    usage_daily_rollups    day x app x model aggregates imported from the
                           harnesses' session logs (falls back to zero when it
                           has no price for a model)
    proxy_request_logs     per-request rows, but only while its local proxy was
                           switched on

Neither is a ledger Sodus can trust for money: the amounts are list-price USD
estimates, imported on CC Switch's own schedule, and a model missing from its
price table is recorded as 0. What they do answer is the question Sodus cannot
currently answer itself: how much usage happens outside Sodus runs, and for
which harnesses. So this module only reads and reports; it never writes rows.

Path: ~/.cc-switch/cc-switch.db, override with SODUS_CC_SWITCH_DB.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".cc-switch" / "cc-switch.db"

TOKEN_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)


def db_path() -> Path:
    return Path(os.environ.get("SODUS_CC_SWITCH_DB") or DEFAULT_DB)


def _empty_bucket() -> dict:
    return {
        "rows": 0,
        "requests": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "estimated_usd": 0.0,
        "zero_cost_requests": 0,
    }


def _add(bucket: dict, row: dict) -> None:
    bucket["rows"] += 1
    requests = int(row.get("request_count") or 1)
    bucket["requests"] += requests
    tokens = bucket["tokens"]
    tokens["input"] += int(row.get("input_tokens") or 0)
    tokens["output"] += int(row.get("output_tokens") or 0)
    tokens["cache_read"] += int(row.get("cache_read_tokens") or 0)
    tokens["cache_write"] += int(row.get("cache_creation_tokens") or 0)
    cost = float(row.get("total_cost_usd") or 0.0)
    bucket["estimated_usd"] += cost
    row_tokens = sum(int(row.get(column) or 0) for column in TOKEN_COLUMNS)
    if cost <= 0 and row_tokens > 0:
        bucket["zero_cost_requests"] += requests


def _totals(apps: dict) -> dict:
    total = {"requests": 0, "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}, "estimated_usd": 0.0, "zero_cost_requests": 0}
    for bucket in apps.values():
        total["requests"] += bucket["requests"]
        total["estimated_usd"] += bucket["estimated_usd"]
        total["zero_cost_requests"] += bucket["zero_cost_requests"]
        for key in total["tokens"]:
            total["tokens"][key] += bucket["tokens"][key]
    return total


def _read_rollups(con: sqlite3.Connection) -> dict:
    rows = con.execute(
        "select date, app_type, request_count, input_tokens, output_tokens,"
        " cache_read_tokens, cache_creation_tokens, total_cost_usd"
        " from usage_daily_rollups"
    ).fetchall()
    apps: dict[str, dict] = {}
    for raw in rows:
        row = dict(raw)
        app = row["app_type"] or "?"
        bucket = apps.setdefault(app, _empty_bucket())
        _add(bucket, row)
    dates = [row["date"] for row in rows if row["date"]]
    return {
        "rows": len(rows),
        "apps": apps,
        "start": min(dates) if dates else "",
        "end": max(dates) if dates else "",
        **_totals(apps),
    }


def _read_proxy(con: sqlite3.Connection) -> dict:
    rows = con.execute(
        "select app_type, input_tokens, output_tokens, cache_read_tokens,"
        " cache_creation_tokens, total_cost_usd, created_at"
        " from proxy_request_logs"
    ).fetchall()
    apps: dict[str, dict] = {}
    stamps = []
    for raw in rows:
        row = dict(raw)
        app = row["app_type"] or "?"
        bucket = apps.setdefault(app, _empty_bucket())
        _add(bucket, row)
        if row["created_at"]:
            stamps.append(float(row["created_at"]))
    window = {
        "start": time.strftime("%Y-%m-%d", time.localtime(min(stamps))) if stamps else "",
        "end": time.strftime("%Y-%m-%d", time.localtime(max(stamps))) if stamps else "",
    }
    return {"rows": len(rows), "apps": apps, **window, **_totals(apps)}


def scan(db_file: Path | None = None) -> dict:
    """Read both stores. Never raises for a missing app or missing table."""
    path = Path(db_file) if db_file is not None else db_path()
    result: dict = {
        "db": str(path),
        "available": False,
        "rollups": None,
        "proxy": None,
        "notes": [
            "金额是 CC Switch 的美元官价估算，不是 9527 站内扣费，不能并入 CNY 账本",
            "缺价格的模型被记成 0，zero_cost_requests 是它的下限缺口",
            "只读探针：不写 observation，不导入任何行",
        ],
    }
    if not path.is_file():
        result["error"] = f"no cc-switch database at {path}"
        return result
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        result["error"] = f"cannot open {path}: {exc}"
        return result
    try:
        for name, reader in (("rollups", _read_rollups), ("proxy", _read_proxy)):
            try:
                result[name] = reader(con)
            except sqlite3.Error as exc:
                result["notes"].append(f"{name}: {exc}")
        result["available"] = bool(result["rollups"] or result["proxy"])
    finally:
        con.close()
    return result
