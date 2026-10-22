"""Hidden checks for budget-currency-admit.

The new queries are judged against a seeded database, and the old contract is
re-asserted here so a rewrite of tests/ cannot hide a behaviour change: the
ceiling still counts CNY only, still stops at the local midnight, and check()
still returns exactly its four keys.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from nodus import budget, db

WORK = Path(__file__).resolve().parent.parent


def _seed(now: float, path: Path, workspace: str = "ws-test") -> None:
    yesterday = budget.day_start(now) - 1
    rows = [
        ("r1", workspace, now - 60, '{"cost": 1.0, "currency": "CNY"}'),
        ("r2", workspace, now - 50, '{"cost": 2.5}'),
        ("r3", workspace, now - 40, '{"cost": 3.0, "currency": "USD"}'),
        ("r4", workspace, now - 30, '{"cost": 0.25, "currency": "usd"}'),
        ("r5", workspace, yesterday, '{"cost": 100.0, "currency": "CNY"}'),
        ("r6", "other-ws", now - 20, '{"cost": 9.0, "currency": "CNY"}'),
        ("r7", workspace, now - 10, "not json"),
        ("r8", workspace, now - 5, '{"cost": "abc", "currency": "CNY"}'),
    ]
    conn = db.connect()
    conn.executemany(
        "INSERT INTO run (id, workspace_id, started_at, usage_json) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def now(tmp_path, monkeypatch):
    monkeypatch.setenv("NODUS_DB", str(tmp_path / "nodus.db"))
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-test")
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "4")
    moment = time.time()
    _seed(moment, tmp_path / "nodus.db")
    return moment


def test_spent_breakdown_groups_by_currency_and_keeps_cny(tmp_path, monkeypatch):
    monkeypatch.setenv("NODUS_DB", str(tmp_path / "nodus.db"))
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-test")
    assert budget.spent_breakdown() == {"CNY": 0.0}


def test_spent_breakdown_values(now):
    assert budget.spent_breakdown(now) == {"CNY": 3.5, "USD": 3.25}
    assert budget.spent(now) == 3.5


def test_check_is_unchanged(now):
    info = budget.check(now)
    assert set(info) == {"limit", "spent", "over", "remaining"}
    assert info == {"limit": 4.0, "spent": 3.5, "over": False, "remaining": 0.5}


def test_refuse_message_is_unchanged(now):
    info = budget.check(now)
    assert budget.refuse_message(info) == (
        "today's spend already reached the daily ceiling "
        "(3.50 / 4.00 CNY); raise budget.daily_cny "
        "or NODUS_DAILY_BUDGET to continue"
    )


def test_admit_within(now):
    decision = budget.admit(0.5, now)
    assert set(decision) == {"allow", "limit", "spent", "projected", "remaining", "reason"}
    assert decision == {
        "allow": True,
        "limit": 4.0,
        "spent": 3.5,
        "projected": 4.0,
        "remaining": 0.5,
        "reason": "within",
    }
    assert isinstance(decision["allow"], bool)


def test_admit_would_exceed(now):
    assert budget.admit(1.0, now) == {
        "allow": False,
        "limit": 4.0,
        "spent": 3.5,
        "projected": 4.5,
        "remaining": 0.5,
        "reason": "would-exceed",
    }


def test_admit_negative_estimate_is_zero(now):
    decision = budget.admit(-100.0, now)
    assert decision["projected"] == 3.5
    assert decision["allow"] is True
    assert decision["reason"] == "within"


def test_admit_already_over(now, monkeypatch):
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "3.5")
    decision = budget.admit(0, now)
    assert decision == {
        "allow": False,
        "limit": 3.5,
        "spent": 3.5,
        "projected": 3.5,
        "remaining": 0.0,
        "reason": "already-over",
    }
    assert isinstance(decision["remaining"], float)


def test_admit_without_a_limit(now, monkeypatch):
    monkeypatch.setenv("NODUS_DAILY_BUDGET", "0")
    assert budget.admit(100.0, now) == {
        "allow": True,
        "limit": 0.0,
        "spent": 3.5,
        "projected": 103.5,
        "remaining": None,
        "reason": "no-limit",
    }


def test_the_existing_suite_is_green():
    assert (WORK / "tests" / "test_budget.py").is_file(), "tests/test_budget.py was removed"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=WORK,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
