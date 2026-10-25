"""A pet utterance is a token bill, not work.

The desktop pet reaches a model through `/llm/chat`, and `llm._record` writes one
row per utterance into the same `run` table the board and the route history
read. With 119 of those newer than every real run, the board's 50-row window
held nothing but chatter and every route looked untested. `run.kind` is the
separation; these tests pin the column, the upgrade path and the filter.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from nodus import config, db

#: The run table as it stood before `kind`. The live database is on the left
#: side of this migration, so the test builds that shape instead of assuming
#: connect() can create it.
OLD_RUN_SCHEMA = """
CREATE TABLE run (
  id TEXT PRIMARY KEY,
  workspace_id TEXT,
  task_id TEXT,
  agent TEXT,
  status TEXT DEFAULT 'queued',
  parent_run_id TEXT,
  cwd TEXT,
  note TEXT,
  effort TEXT,
  session_ref_json TEXT,
  usage_json TEXT,
  started_at REAL,
  ended_at REAL,
  error TEXT
)
"""

WORKSPACE = "ws-run-kind"


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", WORKSPACE)
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def _old_database(home, rows: list[tuple[str, str | None, str | None]]) -> None:
    """A pre-kind database with (id, note, usage_json) rows."""
    conn = sqlite3.connect(home / "nodus.sqlite3")
    conn.execute(OLD_RUN_SCHEMA)
    conn.executemany(
        "INSERT INTO run(id, workspace_id, agent, status, note, usage_json, started_at)"
        " VALUES(?,?,?,?,?,?,?)",
        [
            (rid, WORKSPACE, "claude", "completed", note, usage, index + 1.0)
            for index, (rid, note, usage) in enumerate(rows)
        ],
    )
    conn.commit()
    conn.close()


def test_a_run_is_work_unless_the_caller_says_otherwise(home):
    task = db.create_task(goal="修好登录")
    run = db.create_run(task["id"], "claude", str(home))
    assert run["kind"] == "work"


def test_a_chat_run_keeps_its_kind(home):
    run = db.create_run("", "claude", str(home), note="桌宠", kind="chat")
    assert run["kind"] == "chat"
    assert db.get_run(run["id"])["kind"] == "chat"


def test_list_runs_can_ask_for_one_kind(home):
    task = db.create_task(goal="修好登录")
    work = db.create_run(task["id"], "claude", str(home))
    chat = db.create_run("", "claude", str(home), note="桌宠", kind="chat")

    assert {row["id"] for row in db.list_runs()} == {work["id"], chat["id"]}
    assert [row["id"] for row in db.list_runs(kind="work")] == [work["id"]]
    assert [row["id"] for row in db.list_runs(kind="chat")] == [chat["id"]]


def test_the_kind_filter_is_applied_before_the_limit(home):
    """The window is the bug: 5 newer chat rows must not hide the one work run."""
    task = db.create_task(goal="修好登录")
    work = db.create_run(task["id"], "claude", str(home))
    for index in range(5):
        db.create_run("", "claude", str(home), note=f"桌宠 {index}", kind="chat")

    assert [row["id"] for row in db.list_runs(limit=1, kind="work")] == [work["id"]]


def test_an_old_database_gains_the_column_and_classifies_the_pet_rows(home):
    _old_database(
        home,
        [
            # The pet writes "桌宠", and the default note is "桌宠 <route>";
            # both shapes exist in the live database.
            ("run-pet-short", "桌宠", None),
            ("run-pet-route", "桌宠 claude", None),
            # A chat smoke test whose note is not about the pet: the ledger
            # source llm._record stamps is the only signal, and it is enough.
            (
                "run-pet-smoke",
                "冒烟测试",
                json.dumps({"cost": 0.1, "currency": "CNY", "source": "llm/chat"}),
            ),
            # Real work: one with a note, one from before notes were written.
            ("run-work", "把 login.py 的 bug 修掉", None),
            ("run-work-null", None, None),
        ],
    )

    assert db.get_run("run-pet-short")["kind"] == "chat"
    assert db.get_run("run-pet-route")["kind"] == "chat"
    assert db.get_run("run-pet-smoke")["kind"] == "chat"
    assert db.get_run("run-work")["kind"] == "work"
    assert db.get_run("run-work-null")["kind"] == "work"

    # And the filter reads what the migration wrote.
    assert {row["id"] for row in db.list_runs(kind="chat")} == {
        "run-pet-short",
        "run-pet-route",
        "run-pet-smoke",
    }
    assert {row["id"] for row in db.list_runs(kind="work")} == {
        "run-work",
        "run-work-null",
    }


def test_the_migration_runs_once_and_does_not_rewrite_work(home):
    _old_database(home, [("run-pet", "桌宠", None), ("run-work", "修好登录", None)])

    # Several connects are what a daemon does; a second pass must not hurt.
    for _ in range(3):
        db.connect().close()

    assert db.get_run("run-pet")["kind"] == "chat"
    assert db.get_run("run-work")["kind"] == "work"
    assert len(db.list_runs(kind="work")) == 1
