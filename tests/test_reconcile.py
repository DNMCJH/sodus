"""Hung or orphaned runs must not stay "running" forever."""

from __future__ import annotations

import time

from sodus import config, db, runner


def test_stale_runs_flags_silent_running_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "green")
    run = db.create_run(task["id"], "dsh", str(tmp_path))
    db.update_run(run["id"], status="running", started_at=time.time() - 5000)

    stale = db.stale_runs(900)
    assert [r["id"] for r in stale] == [run["id"]]


def test_stale_runs_keeps_active_run(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "green")
    run = db.create_run(task["id"], "dsh", str(tmp_path))
    db.update_run(run["id"], status="running", started_at=time.time() - 5000)
    db.append_event(run["id"], 1, "run.started", {})

    assert db.stale_runs(900) == []


def test_stale_runs_ignores_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "green")
    run = db.create_run(task["id"], "dsh", str(tmp_path))
    db.update_run(
        run["id"], status="completed", started_at=time.time() - 5000, ended_at=time.time()
    )
    assert db.stale_runs(900) == []


def test_reconcile_marks_orphan_failed_with_event(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("demo", "green")
    run = db.create_run(task["id"], "dsh", str(tmp_path))
    db.update_run(run["id"], status="running", started_at=time.time() - 5000)

    fixed = runner.RUNNER.reconcile(900)
    assert [r["id"] for r in fixed] == [run["id"]]
    after = db.get_run(run["id"])
    assert after["status"] == "failed"
    assert "orphaned" in after["error"]
    assert db.events_after(run["id"])[-1]["type"] == "run.failed"
