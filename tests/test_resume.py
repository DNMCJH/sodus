"""Resuming: continue the same vendor session instead of redoing the work.

A run that dies before finalisation used to leave no session id at all, so the
only option was a fresh worker. The runner now persists the id the moment the
CLI announces it; these tests pin that, and the CLI paths that consume it.
"""

from __future__ import annotations

import json

import pytest

from sodus import config, db
from sodus.cli import build_parser
from sodus.cli.commands import tasks as tasks_cmd


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-resume")
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def _seed(rid: str, *, session: str | None = None, route: str = "claude") -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO run(id, workspace_id, task_id, agent, status, route, cwd,"
            " model, effort, session_ref_json, started_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                rid,
                "ws-resume",
                "task-a",
                "claude",
                "completed",
                route,
                r"C:\work",
                "claude-opus-5-5",
                "high",
                json.dumps({"provider": "claude", "session_id": session})
                if session
                else None,
                1.0,
            ),
        )


def test_session_ref_is_persisted_only_while_empty(home):
    _seed("run-early")
    ref = {"provider": "claude", "session_id": "s-1", "cwd": ""}

    assert db.set_run_session_ref("run-early", ref) is True
    stored = json.loads(db.get_run("run-early")["session_ref_json"])
    assert stored["session_id"] == "s-1"

    # Finalisation keeps the last word: a later write must not overwrite it.
    assert db.set_run_session_ref("run-early", {"session_id": "s-2"}) is False
    assert json.loads(db.get_run("run-early")["session_ref_json"])["session_id"] == "s-1"


def test_set_session_ref_ignores_empty_values(home):
    _seed("run-empty")
    assert db.set_run_session_ref("run-empty", {"provider": "claude"}) is False
    assert db.get_run("run-empty")["session_ref_json"] is None


def test_run_continue_posts_the_previous_session(monkeypatch):
    posted: dict = {}

    def fake_call(method, path, body=None, retries=2):
        if method == "GET" and path == "/runs/run-1":
            return {
                "id": "run-1",
                "task_id": "task-a",
                "route": "claude",
                "cwd": r"C:\work",
                "model": "claude-opus-5-5",
                "effort": "high",
                "session_ref_json": json.dumps({"session_id": "s-1"}),
            }
        if method == "POST" and path == "/runs":
            posted.update(body)
            return {"id": "run-2", "route": body.get("route")}
        raise AssertionError((method, path))

    monkeypatch.setattr(tasks_cmd, "call", fake_call)
    monkeypatch.setattr(tasks_cmd, "watch", lambda rid: 0)
    args = build_parser().parse_args(["run", "--continue", "run-1"])

    assert tasks_cmd.run(args) == 0
    assert posted["session_id"] == "s-1"
    assert posted["session_mode"] == "resume"
    assert posted["task_id"] == "task-a"
    assert posted["parent_run_id"] == "run-1"
    assert posted["route"] == "claude"
    assert posted["cwd"] == r"C:\work"


def test_run_continue_without_a_session_explains_itself(monkeypatch, capsys):
    def fake_call(method, path, body=None, retries=2):
        return {
            "id": "run-1",
            "task_id": "task-a",
            "route": "claude",
            "session_ref_json": None,
        }

    monkeypatch.setattr(tasks_cmd, "call", fake_call)
    monkeypatch.setattr(tasks_cmd, "watch", lambda rid: 0)
    args = build_parser().parse_args(["run", "task-a", "--continue", "run-1"])

    assert tasks_cmd.run(args) == 1
    assert "没有可续的会话" in capsys.readouterr().err


def test_run_resume_creates_a_task_when_none_was_given(monkeypatch):
    calls: list[tuple[str, str, dict | None]] = []

    def fake_call(method, path, body=None, retries=2):
        calls.append((method, path, body))
        if method == "POST" and path == "/tasks":
            return {"id": "task-new"}
        if method == "POST" and path == "/runs":
            return {"id": "run-9", "route": body.get("route")}
        raise AssertionError((method, path))

    monkeypatch.setattr(tasks_cmd, "call", fake_call)
    monkeypatch.setattr(tasks_cmd, "watch", lambda rid: 0)
    args = build_parser().parse_args(
        ["run", "--resume", "sess-9", "--route", "claude", "--note", "继续"]
    )

    assert tasks_cmd.run(args) == 0
    posted = next(body for method, path, body in calls if path == "/runs")
    assert posted["session_id"] == "sess-9"
    assert posted["session_mode"] == "resume"
    assert posted["task_id"] == "task-new"
    assert posted["route"] == "claude"
