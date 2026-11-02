"""One definition of "today": which run rows every panel reads.

The money rules were already in `ledger`; this pins the row set. The board's
money, its usage snapshot, the CLI's stats and the daily ceiling each used to
write their own WHERE clause, so "today" could be one set of runs on the page
and another in a report. They now call `db.runs_in_window`.
"""

from __future__ import annotations

import json
import time

from nodus import budget, config, db, ledger, server, usage


def _run(task, minutes_ago: float, usage_obj: dict, *, status: str = "completed"):
    started = time.time() - minutes_ago * 60
    run = db.create_run(task["id"], "claude", "")
    db.update_run(
        run["id"],
        status=status,
        started_at=started,
        ended_at=started + 5 if status != "running" else None,
        usage_json=json.dumps(usage_obj),
    )
    return run


def _task(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    return db.create_task("demo", "pytest green")


def test_runs_in_window_is_half_open_and_workspace_scoped(tmp_path, monkeypatch):
    task = _task(tmp_path, monkeypatch)
    now = time.time()
    inside = _run(task, 10, {"cost": 1.0, "currency": "CNY"})
    before = _run(task, 60 * 30, {"cost": 2.0, "currency": "CNY"})
    rows = db.runs_in_window(now - 3600, now)
    assert [row["id"] for row in rows] == [inside["id"]]
    assert before["id"] not in {row["id"] for row in rows}


def test_live_window_keeps_a_run_in_flight_before_the_window(tmp_path, monkeypatch):
    task = _task(tmp_path, monkeypatch)
    running = _run(task, 60 * 30, {"cost": 0.2}, status="running")
    rows = db.runs_in_window(time.time() - 3600, live=True)
    assert running["id"] in {row["id"] for row in rows}
    # Without live=True the same row is not "today": it did not start today.
    assert running["id"] not in {
        row["id"] for row in db.runs_in_window(time.time() - 3600)
    }


def test_board_cli_and_ceiling_read_one_row_set(tmp_path, monkeypatch):
    task = _task(tmp_path, monkeypatch)
    now = time.time()
    _run(task, 5, {"cost": 1.0, "currency": "CNY"})
    _run(task, 15, {"cost": 0.5})  # no recorded currency
    _run(task, 60 * 30, {"cost": 9.0, "currency": "CNY"})  # yesterday

    start = budget.day_start(now)
    rows = db.runs_in_window(start)
    assert len(rows) == 2
    assert {row["id"] for row in usage._rows(start)} == {row["id"] for row in rows}

    conn = db.connect()
    try:
        board = server._today(conn, start)
    finally:
        conn.close()
    assert board["runs"] == 2
    assert board["totals"] == {"CNY": 1.0, ledger.UNKNOWN_CURRENCY: 0.5}
    assert usage.summary("day", now=now)["totals"]["runs"] == 2

    # The ceiling keeps its own reading of an unnamed amount: CNY, because
    # every row this workspace writes is CNY and a ceiling that ignores spend
    # is not a ceiling. The board keeps the other one: an amount with no
    # currency is not labelled USD. Different readings, same rows.
    assert budget.spent(now) == 1.5
