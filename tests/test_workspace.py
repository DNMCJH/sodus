"""Workspace isolation: reads must not cross the active workspace."""

from __future__ import annotations

from sodus import config, db


def test_tasks_and_runs_are_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-a")
    task_a = db.create_task("alpha")
    run_a = db.create_run(task_a["id"], "claude", str(tmp_path))

    monkeypatch.setenv("SODUS_WORKSPACE", "ws-b")
    task_b = db.create_task("beta")

    assert db.get_task(task_a["id"]) == {}
    assert db.get_task(task_b["id"])["id"] == task_b["id"]
    assert [t["id"] for t in db.list_tasks()] == [task_b["id"]]
    assert db.get_run(run_a["id"]) == {}
    assert db.list_runs() == []

    monkeypatch.setenv("SODUS_WORKSPACE", "ws-a")
    assert db.get_task(task_a["id"])["id"] == task_a["id"]
    assert db.get_run(run_a["id"])["id"] == run_a["id"]


def test_artifacts_and_handoffs_are_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-a")
    task = db.create_task("alpha")
    run = db.create_run(task["id"], "claude", str(tmp_path))
    db.add_artifact(run["id"], task["id"], "file", str(tmp_path / "a.txt"), "hash")
    db.add_handoff(task["id"], run["id"], str(tmp_path / "h.md"), "hash")

    monkeypatch.setenv("SODUS_WORKSPACE", "ws-b")
    assert db.artifacts_for(run["id"]) == []
    assert db.artifacts_for_task(task["id"]) == []
    assert db.latest_handoff(task["id"]) == {}

    monkeypatch.setenv("SODUS_WORKSPACE", "ws-a")
    assert len(db.artifacts_for(run["id"])) == 1
    assert db.latest_handoff(task["id"])["path"].endswith("h.md")
