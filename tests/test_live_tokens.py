"""Live rows: one summation for mid-flight usage, and it lives in ledger."""

from __future__ import annotations

import json
import time

from sodus import config, db, ledger, server


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


def test_sum_events_aggregates_db_usage_events(tmp_path, monkeypatch):
    run = _running_run(tmp_path, monkeypatch)
    for seq, payload in enumerate(
        [
            {"input_tokens": 4, "output_tokens": 2, "cost": 0.1},
            {"input_tokens": 6, "output_tokens": 8, "cost": 0.2},
        ],
        start=1,
    ):
        db.append_event(run["id"], seq, "usage", payload)
    with db.connect() as conn:
        so_far = ledger.sum_events(
            ev["payload_json"]
            for ev in conn.execute(
                "SELECT payload_json FROM event WHERE run_id=? AND type='usage'",
                (run["id"],),
            )
        )
    assert (so_far.tokens.input, so_far.tokens.output) == (10, 10)
    assert round(so_far.cost.amount, 6) == 0.3
