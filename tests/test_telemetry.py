"""Cost has one reader: `ledger`. These tests compare the old readers to it.

`telemetry.usage_summary` (the legacy `nodus usage --days` path) used to parse
`usage_json` itself and invented `USD` when the currency was missing, while the
board and `nodus stats` put the same money under `"-"`. Now all of them call
`ledger`; the numbers must be identical, and an unnamed currency must stay
unnamed.
"""

from __future__ import annotations

import json
import time

from nodus import config, db, ledger, server, telemetry, usage


def _seed(route: str, usage_json, *, status: str = "completed", started: float) -> dict:
    run = db.create_run(task_id="task-cost", agent=route, cwd=".", route=route)
    db.update_run(
        run["id"],
        status=status,
        started_at=started,
        usage_json=json.dumps(usage_json) if isinstance(usage_json, dict) else usage_json,
    )
    return run


def _cost_by_route(rows: list[dict]) -> dict[str, dict[str, float]]:
    data = ledger.summarize(rows, group_by="route")
    return {
        group["key"]: {currency: money["total"] for currency, money in group["cost"].items()}
        for group in data["groups"]
    }


def test_legacy_telemetry_and_the_cli_report_the_same_money(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-cost-paths")
    now = time.time()
    _seed("claude", {"cost": 2.0, "currency": "CNY", "input_tokens": 100, "output_tokens": 10}, started=now)
    _seed("claude", {"cost": 1.5, "input_tokens": 50}, started=now)
    _seed("dsh", {"cost": 3.0, "currency": "USD", "input_tokens": 7}, started=now)
    _seed("dsh", None, status="failed", started=now)

    rows = db.list_runs(limit=10000)
    legacy = telemetry.usage_summary(days=1)
    report = usage.summary(period="all", group_by="route")

    expected = _cost_by_route(rows)
    assert {route: bucket["costs"] for route, bucket in legacy.items()} == expected
    assert {
        group["key"]: {currency: money["total"] for currency, money in group["cost"].items()}
        for group in report["groups"]
    } == expected
    # Tokens too, not just money: same reader, same counts.
    for group in report["groups"]:
        bucket = legacy[group["key"]]
        assert bucket["in"] == group["tokens"]["prompt"]
        assert bucket["out"] == group["tokens"]["completion"]
        assert bucket["cache_read"] == group["tokens"]["cache_read"]


def test_a_missing_currency_is_unknown_everywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-cost-unknown")
    now = time.time()
    _seed("claude", {"cost": 1.5}, started=now)

    legacy = telemetry.usage_summary(days=1)
    assert legacy["claude"]["costs"] == {ledger.UNKNOWN_CURRENCY: 1.5}
    assert "USD" not in legacy["claude"]["costs"]

    report = usage.summary(period="all")
    assert report["totals"]["cost"][ledger.UNKNOWN_CURRENCY]["total"] == 1.5
    assert "USD" not in report["totals"]["cost"]

    conn = db.connect()
    try:
        today = server._today(conn, now - 60)
    finally:
        conn.close()
    assert today["totals"] == {ledger.UNKNOWN_CURRENCY: 1.5}


def test_telemetry_keeps_a_real_usd_run_apart_from_an_unnamed_one(tmp_path, monkeypatch):
    # The old bug was not "USD exists": it was "a missing currency becomes
    # USD". A run that really recorded USD must still be its own bucket.
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-cost-usd")
    now = time.time()
    _seed("claude", {"cost": 1.5}, started=now)
    _seed("claude", {"cost": 2.0, "currency": "USD"}, started=now)

    costs = telemetry.usage_summary(days=1)["claude"]["costs"]
    assert costs == {ledger.UNKNOWN_CURRENCY: 1.5, "USD": 2.0}
    assert costs[ledger.UNKNOWN_CURRENCY] != costs["USD"]
