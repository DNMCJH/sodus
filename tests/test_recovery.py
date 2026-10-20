"""Recovery: a daemon restart must not leave corpses or lose written files."""

from __future__ import annotations

import time

from nodus import config, db, runner


def test_reap_orphans_fails_live_runs_and_keeps_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    work = tmp_path / "work"
    work.mkdir()
    task = db.create_task("t")
    run = db.create_run(task["id"], "dsh", str(work), None, "", "", "")
    db.update_run(run["id"], status="running", started_at=time.time() - 5)
    (work / "out.md").write_text("the worker got this far", encoding="utf-8")

    reaped = runner.RUNNER.reap_orphans()

    assert [r["id"] for r in reaped] == [run["id"]]
    stored = db.get_run(run["id"])
    assert stored["status"] == "failed"
    assert "orphaned" in stored["error"]
    paths = [a["path"] for a in db.artifacts_for(run["id"])]
    assert str(work / "out.md") in paths


def test_reap_orphans_leaves_finished_runs_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    db.update_run(run["id"], status="completed", ended_at=time.time())

    assert runner.RUNNER.reap_orphans() == []
    assert db.get_run(run["id"])["status"] == "completed"


def test_the_second_start_has_nothing_to_reap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    task = db.create_task("t")
    db.create_run(task["id"], "dsh", str(tmp_path))

    assert len(runner.RUNNER.reap_orphans()) == 1
    assert runner.RUNNER.reap_orphans() == []
