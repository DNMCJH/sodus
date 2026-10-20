"""Run status is an exit code; verification is a separate fact."""

from __future__ import annotations

from nodus import config, db, runner


def _event(rid: str, etype: str) -> None:
    db.append_event(rid, db.next_seq(rid), etype, {"ok": etype == "verify.green"})


def test_verify_marks_come_from_test_events_not_status(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    task = db.create_task("t")
    ok_run = db.create_run(task["id"], "dsh", str(tmp_path))
    red_run = db.create_run(task["id"], "dsh", str(tmp_path))
    silent = db.create_run(task["id"], "dsh", str(tmp_path))
    for run, status in ((ok_run, "completed"), (red_run, "completed"), (silent, "completed")):
        db.update_run(run["id"], status=status, ended_at=1.0)

    _event(ok_run["id"], "verify.red")
    _event(ok_run["id"], "verify.green")
    _event(red_run["id"], "verify.red")
    _event(silent["id"], "verify.fix1")  # a phase name, not a verdict

    states = db.verify_states([ok_run["id"], red_run["id"], silent["id"]])
    assert states[ok_run["id"]] == "ok"
    assert states[red_run["id"]] == "red"
    assert silent["id"] not in states


def test_empty_run_list_is_not_a_query():
    assert db.verify_states([]) == {}


def test_handoff_says_when_nothing_was_checked():
    task = {"id": "task-1", "title": "x", "goal": "g", "acceptance": "a"}
    text = runner.build_handoff(task, "run-1", "dsh", "completed", "done", [], "", "none")
    assert "没有跑测试" in text
    assert "只来自 agent 自述" in text


def test_handoff_reports_a_passing_check_without_caveats():
    task = {"id": "task-1", "title": "x", "goal": "g", "acceptance": "a"}
    text = runner.build_handoff(task, "run-1", "dsh", "completed", "done", [], "", "ok")
    assert "测试通过" in text
    assert "只来自 agent 自述" not in text
