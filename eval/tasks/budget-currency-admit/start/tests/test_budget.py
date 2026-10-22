"""The budget module's existing contract: CNY only, today only, this workspace."""

from __future__ import annotations

import json
import time

import pytest

from nodus import budget, db


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setenv("NODUS_DB", str(tmp_path / "nodus.db"))
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-test")
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "10")
    monkeypatch.delenv("NODUS_CONFIG", raising=False)
    return tmp_path


def seed(rows):
    conn = db.connect()
    conn.executemany(
        "INSERT INTO run (id, workspace_id, started_at, usage_json) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_limit_reads_the_environment(wired, monkeypatch):
    assert budget.limit() == 10.0
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "nonsense")
    assert budget.limit() == 0.0


def test_limit_falls_back_to_config(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"budget": {"daily_cny": 7}}), encoding="utf-8")
    monkeypatch.delenv("NODUS_DAILY_BUDGET", raising=False)
    monkeypatch.setenv("NODUS_CONFIG", str(path))
    assert budget.limit() == 7.0


def test_spent_counts_only_todays_cny_for_this_workspace(wired):
    now = time.time()
    yesterday = budget.day_start(now) - 1
    seed(
        [
            ("a", "ws-test", now - 60, '{"cost": 1.25, "currency": "CNY"}'),
            ("b", "ws-test", now - 30, '{"cost": 2.75}'),
            ("c", "ws-test", now - 20, '{"cost": 9.0, "currency": "USD"}'),
            ("d", "ws-test", yesterday, '{"cost": 100.0, "currency": "CNY"}'),
            ("e", "other", now - 10, '{"cost": 50.0, "currency": "CNY"}'),
            ("f", "ws-test", now - 5, "not json"),
            ("g", "ws-test", now - 4, '{"cost": "abc"}'),
        ]
    )
    assert budget.spent(now) == 4.0


def test_check_reports_limit_spent_and_remaining(wired):
    now = time.time()
    seed([("a", "ws-test", now - 60, '{"cost": 4.0}')])
    info = budget.check(now)
    assert info == {"limit": 10.0, "spent": 4.0, "over": False, "remaining": 6.0}


def test_check_is_over_at_the_limit(wired):
    now = time.time()
    seed([("a", "ws-test", now - 60, '{"cost": 10.0}')])
    info = budget.check(now)
    assert info["over"] is True
    assert info["remaining"] == 0.0


def test_refuse_message_is_stable():
    info = {"limit": 10.0, "spent": 12.5, "over": True, "remaining": 0.0}
    assert budget.refuse_message(info) == (
        "today's spend already reached the daily ceiling "
        "(12.50 / 10.00 CNY); raise budget.daily_cny "
        "or NODUS_DAILY_BUDGET to continue"
    )


def test_day_start_is_local_midnight():
    start = budget.day_start()
    local = time.localtime(start)
    assert (local.tm_hour, local.tm_min, local.tm_sec) == (0, 0, 0)
    assert start <= time.time()
