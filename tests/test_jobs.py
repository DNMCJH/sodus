"""Daemon jobs: a pipeline outlives the terminal, and failure always closes it."""

from __future__ import annotations

import time

import pytest

from nodus import config, db, jobs


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_a_pipeline_row_starts_queued_and_becomes_visible(home):
    row = db.create_pipeline("tdd", {"task_id": "task-1"}, label="t")
    assert row["status"] == "queued"
    assert [r["id"] for r in db.list_pipelines()] == [row["id"]]
    db.update_pipeline(row["id"], status="running", started_at=time.time())
    assert [r["id"] for r in db.list_pipelines("running")] == [row["id"]]


def test_a_daemon_restart_closes_what_it_left_running(home):
    running = db.create_pipeline("fix", {"target_run_id": "run-1"})
    db.update_pipeline(running["id"], status="running")
    done = db.create_pipeline("review", {"target_run_id": "run-2"})
    db.update_pipeline(done["id"], status="completed")

    closed = jobs.reap(db.list_pipelines("running"))

    assert [r["id"] for r in closed] == [running["id"]]
    row = db.get_pipeline(running["id"])
    assert row["status"] == "failed"
    assert "daemon restarted" in (row["error"] or "")
    assert db.get_pipeline(done["id"])["status"] == "completed"


def test_a_broken_job_always_closes_its_row(home):
    """Missing required params must produce a failed pipeline, not a stuck thread."""
    row = jobs.JOBS.submit("tdd", {}, label="broken")
    deadline = time.time() + 30
    while time.time() < deadline:
        current = db.get_pipeline(row["id"])
        if current["status"] in ("failed", "completed", "stopped"):
            break
        time.sleep(0.1)
    final = db.get_pipeline(row["id"])
    assert final["status"] == "failed"
    assert final["error"]
    types = [e["type"] for e in db.events_after(row["id"])]
    assert "pipeline.started" in types
    assert "pipeline.failed" in types


def test_an_unknown_kind_is_refused_before_a_row_exists(home):
    with pytest.raises(ValueError, match="unknown pipeline"):
        jobs.JOBS.submit("deploy", {})


def test_stopping_an_unknown_job_is_harmless(home):
    assert jobs.JOBS.stop("pipe-nope") is True
