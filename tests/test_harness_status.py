"""Terminals: five harnesses, one read-only probe, one shape.

The probe is the half that can be wrong quietly -- a wrong path means an open
terminal reads as "never used". These tests build fake session-log trees and a
throwaway run table, so the layouts, the freshness rule and the run-table
aggregation are pinned without reading the operator's real logs.
"""

from __future__ import annotations

import inspect
import json
import os
import time

import pytest

from nodus import config, db, harness_status, server

WS = "ws-terminals"


def _local(month: int, day: int, hour: int = 0) -> float:
    return time.mktime((2026, month, day, hour, 0, 0, 0, 0, -1))


NOW = _local(10, 25, 12)
TODAY = _local(10, 25, 9)
YESTERDAY = _local(10, 24, 9)


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", WS)
    (tmp_path / "home").mkdir()
    return tmp_path


def _roots(tmp_path) -> dict:
    """One empty log root per harness, under a temp dir."""
    roots = {}
    for harness in harness_status.HARNESSES:
        directory = tmp_path / "logs" / harness.key
        directory.mkdir(parents=True, exist_ok=True)
        roots[harness.key] = directory
    return roots


def _touch(path, at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    os.utime(path, (at, at))


def _installed(name: str) -> str:
    return "C:\\bin\\" + name + ".CMD"


def _probe(tmp_path, roots, *, now: float = NOW, home=None, which=_installed):
    return harness_status.probe(
        now=now,
        home=home or tmp_path / "user",
        roots_override=roots,
        which=which,
    )


def test_the_five_terminals_come_in_one_fixed_order(tmp_path):
    rows = _probe(tmp_path, _roots(tmp_path), which=lambda name: "")
    assert [row["key"] for row in rows] == ["dsh", "claude", "codex", "opencode", "kimi"]
    assert [row["name"] for row in rows] == [
        "DSH", "Claude Code", "Codex", "OpenCode", "Kimi",
    ]
    # 一视同仁: one shape, so the page needs no per-harness branch.
    assert len({frozenset(row) for row in rows}) == 1


def test_installed_is_which_plus_a_known_install_path(tmp_path):
    rows = _probe(
        tmp_path,
        _roots(tmp_path),
        which=lambda name: _installed(name) if name == "claude" else "",
    )
    by_key = {row["key"]: row for row in rows}
    assert by_key["claude"]["installed"] is True
    assert by_key["claude"]["binary"] == _installed("claude")
    assert by_key["codex"]["installed"] is False
    assert by_key["codex"]["state"] == "missing"

    # Kimi ships its exe inside its own home, so it is installed without PATH.
    fake_home = tmp_path / "user"
    kimi = fake_home / ".kimi-code" / "bin" / "kimi.exe"
    kimi.parent.mkdir(parents=True)
    kimi.write_text("", encoding="utf-8")
    rows = _probe(tmp_path, _roots(tmp_path), home=fake_home, which=lambda name: "")
    by_key = {row["key"]: row for row in rows}
    assert by_key["kimi"]["installed"] is True
    assert by_key["kimi"]["binary"].endswith("kimi.exe")
    assert by_key["dsh"]["installed"] is False


def test_a_recent_log_write_is_running_and_an_old_one_is_idle(tmp_path):
    roots = _roots(tmp_path)
    _touch(roots["claude"] / "projects" / "p" / "s.jsonl", NOW - 60)
    _touch(
        roots["codex"] / "sessions" / "2026" / "10" / "24" / "rollout-x.jsonl",
        NOW - 86400,
    )
    rows = _probe(tmp_path, roots)
    by_key = {row["key"]: row for row in rows}
    assert by_key["claude"]["state"] == "running"
    assert by_key["claude"]["running"] is True
    assert by_key["claude"]["last_active"] == pytest.approx(NOW - 60, abs=1)
    assert by_key["codex"]["state"] == "idle"
    assert by_key["codex"]["running"] is False
    assert by_key["dsh"]["last_active"] is None
    assert by_key["dsh"]["state"] == "idle"


def test_the_newest_file_wins_and_the_shm_is_not_activity(tmp_path):
    roots = _roots(tmp_path)
    _touch(roots["claude"] / "projects" / "a" / "old.jsonl", NOW - 1000)
    _touch(roots["claude"] / "projects" / "b" / "new.jsonl", NOW - 10)
    rows = _probe(tmp_path, roots)
    claude = {row["key"]: row for row in rows}["claude"]
    assert claude["last_active"] == pytest.approx(NOW - 10, abs=1)

    # OpenCode keeps sessions in one SQLite file: -wal carries the fresh write,
    # -shm is touched by a mere read and must not read as "just active".
    _touch(roots["opencode"] / "opencode.db", NOW - 500)
    _touch(roots["opencode"] / "opencode.db-wal", NOW - 20)
    _touch(roots["opencode"] / "opencode.db-shm", NOW)
    rows = _probe(tmp_path, roots)
    opencode = {row["key"]: row for row in rows}["opencode"]
    assert opencode["last_active"] == pytest.approx(NOW - 20, abs=1)


def test_a_missing_log_directory_is_an_answer_not_an_error(tmp_path):
    roots = {harness.key: tmp_path / "gone" / harness.key for harness in harness_status.HARNESSES}
    rows = _probe(tmp_path, roots)
    assert [row["last_active"] for row in rows] == [None] * 5
    assert [row["state"] for row in rows] == ["idle"] * 5


def test_default_roots_are_the_documented_locations(tmp_path, monkeypatch):
    monkeypatch.delenv("NODUS_DSH_HOME", raising=False)
    roots = harness_status.roots(tmp_path)
    assert roots["claude"] == tmp_path / ".claude"
    assert roots["codex"] == tmp_path / ".codex"
    assert roots["opencode"] == tmp_path / ".local" / "share" / "opencode"
    assert roots["kimi"] == tmp_path / ".kimi-code"
    assert roots["dsh"] == harness_status.observe.root()


def test_routes_share_a_terminal_and_a_foreign_agent_is_not_one():
    assert harness_status.harness_key("opencode-glm") == "opencode"
    assert harness_status.harness_key("opencode-gemini") == "opencode"
    assert harness_status.harness_key("OpenCode") == "opencode"
    assert harness_status.harness_key("claude") == "claude"
    assert harness_status.harness_key("minimax") == ""
    assert harness_status.harness_key("") == ""


def test_the_probe_never_runs_a_process_or_opens_a_socket():
    # The page must stay a read: no CLI is invoked, no socket is opened. This
    # pins the imports, not the prose (the docstring names what it refuses to do).
    source = inspect.getsource(harness_status)
    for forbidden in ("import subprocess", "import socket", "urlopen(", "requests."):
        assert forbidden not in source


# ---- today's numbers, from the run table ----------------------------------


def _seed(
    agent: str,
    *,
    started: float = TODAY,
    kind: str = "work",
    status: str = "completed",
    session: str = "",
    cost: float | None = None,
    currency: str = "CNY",
) -> dict:
    task = db.create_task(goal="写一个文件", acceptance="pytest -q")
    run = db.create_run(task["id"], agent, str(config.HOME / "work"), route=agent)
    fields = {
        "status": status,
        "started_at": started,
        "kind": kind,
        "session_ref_json": json.dumps({"session_id": session}) if session else "",
    }
    if cost is not None:
        fields["usage_json"] = json.dumps({"cost": cost, "currency": currency})
    db.update_run(run["id"], **fields)
    return run


def test_today_is_counted_per_terminal_from_the_run_table(home):
    _seed("claude", session="s-1", cost=1.5)
    _seed("claude", cost=0.5)
    _seed("claude", kind="chat", cost=0.25)
    _seed("claude", started=YESTERDAY, cost=99.0)
    _seed("codex", session="c-1", cost=2.0)
    _seed("codex", session="c-1")
    _seed("opencode-glm", session="o-1", cost=0.1)
    _seed("opencode-gemini", session="o-2", cost=0.2)
    _seed("minimax", cost=5.0)

    conn = db.connect()
    try:
        rows = harness_status.snapshot(
            conn,
            now=NOW,
            home=home / "user",
            roots_override=_roots(home),
            which=_installed,
        )
    finally:
        conn.close()
    by_key = {row["key"]: row for row in rows}

    # Work only: the pet's chatter is a bill, not an attempt.
    assert by_key["claude"]["today"]["runs"] == 2
    # Two distinct vendor conversations; a run with no ref counts as its own.
    assert by_key["claude"]["today"]["sessions"] == 2
    # Money includes the chat row and excludes yesterday.
    assert by_key["claude"]["today"]["cost"]["CNY"] == pytest.approx(2.25)

    assert by_key["codex"]["today"]["runs"] == 2
    assert by_key["codex"]["today"]["sessions"] == 1
    assert by_key["codex"]["today"]["cost"]["CNY"] == pytest.approx(2.0)

    # Two routes through one CLI are one terminal.
    assert by_key["opencode"]["today"]["runs"] == 2
    assert by_key["opencode"]["today"]["sessions"] == 2
    assert by_key["opencode"]["today"]["cost"]["CNY"] == pytest.approx(0.3)

    # A harness nobody dispatched today says zero, not nothing.
    assert by_key["dsh"]["today"] == {"runs": 0, "sessions": 0, "cost": {}}
    assert by_key["kimi"]["today"] == {"runs": 0, "sessions": 0, "cost": {}}
    # A route that is not one of the five terminals is not on the panel.
    assert "minimax" not in by_key


def test_a_live_run_is_presence_even_with_no_log(home):
    _seed("codex", status="queued")
    conn = db.connect()
    try:
        rows = harness_status.snapshot(
            conn,
            now=NOW,
            home=home / "user",
            roots_override=_roots(home),
            which=_installed,
        )
    finally:
        conn.close()
    by_key = {row["key"]: row for row in rows}
    assert by_key["codex"]["running"] is True
    assert by_key["codex"]["state"] == "running"
    assert by_key["claude"]["state"] == "idle"


def test_a_run_today_proves_the_cli_exists_even_when_path_disagrees(home):
    # The daemon's PATH and this process's PATH are not the same thing; a
    # harness that did work today must not read as 未安装.
    _seed("claude", session="s-1")
    conn = db.connect()
    try:
        rows = harness_status.snapshot(
            conn,
            now=NOW,
            home=home / "user",
            roots_override=_roots(home),
            which=lambda name: "",
        )
    finally:
        conn.close()
    by_key = {row["key"]: row for row in rows}
    assert by_key["claude"]["installed"] is True
    assert by_key["claude"]["state"] == "idle"
    assert by_key["codex"]["installed"] is False
    assert by_key["codex"]["state"] == "missing"


# ---- the board's contract -------------------------------------------------


def test_board_carries_the_terminals(home, monkeypatch):
    terminal = {
        "key": "dsh",
        "name": "DSH",
        "installed": True,
        "running": True,
        "state": "running",
        "last_active": NOW - 5,
        "binary": "C:\\bin\\dsh.CMD",
        "log_root": "C:\\dsh\\home\\sessions",
        "today": {"runs": 1, "sessions": 1, "cost": {"CNY": 0.01}},
    }
    monkeypatch.setattr(
        server.harness_status, "snapshot", lambda conn, **kwargs: [terminal]
    )
    board = server.board_state(now=NOW)
    assert board["terminals"] == [terminal]


def test_a_broken_probe_does_not_take_the_board_down(home, monkeypatch):
    def boom(conn, **kwargs):
        raise RuntimeError("log directory vanished")

    monkeypatch.setattr(server.harness_status, "snapshot", boom)
    board = server.board_state(now=NOW)
    assert board["terminals"] == []
