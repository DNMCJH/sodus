"""Live rows: one summation for mid-flight usage, and it lives in ledger."""

from __future__ import annotations

import json
import time

from nodus import config, db, ledger, server


def _running_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "pytest green")
    return db.create_run(task["id"], "claude", str(tmp_path))


def test_sum_events_adds_recorded_fields_and_keeps_absent_ones_absent():
    usage = ledger.sum_events(
        [
            json.dumps({"input_tokens": 10, "output_tokens": 5, "cost": 0.25}),
            json.dumps({"input_tokens": 20, "output_tokens": 7, "cost": 0.5}),
        ]
    )
    assert (usage.tokens.input, usage.tokens.output) == (30, 12)
    assert usage.tokens.cache_read is None  # never recorded, not a zero
    assert usage.cost.amount == 0.75
    assert usage.cost.currency is None  # the events do not record a currency


def test_sum_events_treats_a_broken_payload_as_nothing():
    usage = ledger.sum_events(["{not json", json.dumps({"output_tokens": 3})])
    assert usage.tokens.input is None
    assert usage.tokens.output == 3


def test_live_rows_sum_usage_events_through_ledger(tmp_path, monkeypatch):
    run = _running_run(tmp_path, monkeypatch)
    for seq, payload in enumerate(
        [
            {"input_tokens": 4, "output_tokens": 2, "cost": 0.1},
            {"input_tokens": 6, "output_tokens": 8, "cost": 0.2},
        ],
        start=1,
    ):
        db.append_event(run["id"], seq, "usage", payload)
    conn = db.connect()
    try:
        rows = server._live_rows(conn, time.time())
    finally:
        conn.close()
    row = next(r for r in rows if r["id"] == run["id"])
    assert row["tokens"] == {"input": 10, "output": 10}
    assert row["partial_cost"] == 0.3
    assert row["partial_currency"] == "USD"


def test_live_rows_without_usage_events_report_nothing(tmp_path, monkeypatch):
    run = _running_run(tmp_path, monkeypatch)
    conn = db.connect()
    try:
        rows = server._live_rows(conn, time.time())
    finally:
        conn.close()
    row = next(r for r in rows if r["id"] == run["id"])
    assert row["tokens"] == {"input": 0, "output": 0}
    assert row["partial_cost"] is None
    assert row["partial_currency"] == ""
