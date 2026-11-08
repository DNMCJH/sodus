"""The watchdog is the first writer of `stalled`: running, but not moving.

The state machine already declared `running -> stalled`; nothing wrote it. These
tests pin the writer and its boundaries: progress is a tool_call / artifact /
status change, the window is strict, only `running` rows move, the lock is
released before the status write, the event names the last real event, a second
sweep is a no-op, and a daemon-owned worker that already acted is stopped.

The board part is the second half of the same claim: a stalled run is `stopped`
(a report), never `needs_you` (a question) or `handling` (the machine at work),
and it does not invent a fifth counter.
"""

from __future__ import annotations

import inspect
import json
import time

import pytest

from nodus import config, db, lifecycle, runner, server, watchdog


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-watchdog")
    monkeypatch.delenv("NODUS_WATCHDOG", raising=False)
    monkeypatch.delenv("NODUS_STALL_SECONDS", raising=False)
    monkeypatch.delenv("NODUS_DAILY_BUDGET", raising=False)
    monkeypatch.setenv("NODUS_AUTO_RECOVER", "1")
    (tmp_path / "home").mkdir()
    return tmp_path


def add_event(run: dict, etype: str, at: float, **payload) -> None:
    seq = db.next_seq(run["id"])
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO event(run_id, seq, type, payload_json, ts) VALUES(?,?,?,?,?)",
            (run["id"], seq, etype, json.dumps(payload, ensure_ascii=False), at),
        )


def add_artifact(run: dict, at: float, rel: str = "src/app.py") -> None:
    cwd = run["cwd"]
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO artifact(id, workspace_id, run_id, task_id, kind, path,"
            " rel_path, hash, summary, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                db.new_id("art"),
                config.workspace_id(),
                run["id"],
                run["task_id"],
                "file",
                str(cwd) + "\\" + rel.replace("/", "\\"),
                rel,
                "h",
                "",
                at,
            ),
        )


def seed(
    home,
    *,
    status: str = "running",
    age: float = 1200.0,
    lock: bool = False,
    events: tuple[tuple[str, float, dict], ...] = (),
    artifacts: tuple[float, ...] = (),
    started_at: float | None = None,
) -> dict:
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    task = db.create_task(goal="修好登录页的报错，让 pytest 通过", acceptance="pytest -q", title="修复登录")
    run = db.create_run(
        task["id"], "claude", str(work), route="claude", provider="relay", lock=lock
    )
    now = time.time()
    start = now - age if started_at is None else started_at
    db.update_run(
        run["id"],
        status=status,
        started_at=start,
        ended_at=None if status in ("running", "stalled") else now - 1,
    )
    for etype, seconds_ago, payload in events:
        add_event(run, etype, now - seconds_ago, **payload)
    for seconds_ago in artifacts:
        add_artifact(run, now - seconds_ago)
    return db.get_run(run["id"])


def stall_events(run: dict) -> list[dict]:
    return [e for e in db.events_after(run["id"]) if e["type"] == "run.stalled"]


# ---- the judgement -----------------------------------------------------


def test_a_silent_running_run_is_marked_stalled(home):
    run = seed(home, age=1200, lock=True)

    result = watchdog.sweep()

    assert [item["run_id"] for item in result["stalled"]] == [run["id"]]
    after = db.get_run(run["id"])
    assert after["status"] == "stalled"
    assert lifecycle.needs_attention(after, {}) == "stalled"
    # The lock is released before the status write; no active lock survives.
    assert db.active_locks() == []
    events = stall_events(run)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["reason"] == "no-substantive-progress"
    assert payload["last_event"] == "run.started"
    assert "无实质进展" in payload["message"]
    assert "20.0 分钟" in payload["message"]
    assert payload["idle_seconds"] == pytest.approx(1200, abs=2)


def test_the_last_tool_call_is_named_in_the_event(home):
    run = seed(
        home,
        age=1300,
        events=(("tool_call", 1200, {"name": "Bash", "input": {"command": "pytest -q"}}),),
    )

    watchdog.sweep()

    payload = stall_events(run)[0]["payload"]
    assert payload["last_event"] == "tool_call（Bash）"
    assert "tool_call（Bash）" in payload["message"]
    assert "20.0 分钟" in payload["message"]


def test_an_artifact_is_progress(home):
    run = seed(home, age=1300, artifacts=(30.0,))

    result = watchdog.sweep()

    assert result["stalled"] == []
    assert db.get_run(run["id"])["status"] == "running"
    assert stall_events(run) == []


def test_a_fresh_tool_call_keeps_the_run_running(home):
    run = seed(home, age=1200, events=(("tool_call", 30, {"name": "Read"}),))

    assert watchdog.sweep()["stalled"] == []
    assert db.get_run(run["id"])["status"] == "running"


def test_the_window_is_strict_and_configurable(home, monkeypatch):
    monkeypatch.setenv("NODUS_STALL_SECONDS", "60")
    run = seed(home, age=1000, started_at=1000.0)

    # Exactly at the window is still given the benefit of the doubt, matching
    # lifecycle._stalled and the runner's "older than" idle check.
    assert watchdog.judge(run, now=1060.0) is None
    finding = watchdog.judge(run, now=1060.5)
    assert finding is not None and finding["idle_seconds"] == pytest.approx(60.5)


def test_completed_failed_and_already_stalled_rows_are_left_alone(home):
    completed = seed(home, status="completed", age=5000)
    failed = seed(home, status="failed", age=5000)
    stalled = seed(home, status="stalled", age=5000)

    result = watchdog.sweep()

    assert result["stalled"] == []
    assert db.get_run(completed["id"])["status"] == "completed"
    assert db.get_run(failed["id"])["status"] == "failed"
    assert db.get_run(stalled["id"])["status"] == "stalled"
    for run in (completed, failed, stalled):
        assert stall_events(run) == []


def test_a_second_sweep_is_a_noop_and_a_restart_does_not_reprocess(home):
    run = seed(home, age=1200)

    assert len(watchdog.sweep()["stalled"]) == 1
    assert watchdog.sweep()["stalled"] == []
    # "Restart" only means a fresh sweep: `stalled` is not in live_runs(), so
    # history is not touched again.
    assert watchdog.sweep()["stalled"] == []
    assert len(stall_events(run)) == 1


def test_an_illegal_transition_is_logged_and_left_alone(home):
    run = seed(home, age=1200)
    finding = watchdog.judge(run)
    assert finding is not None
    db.update_run(run["id"], status="completed")
    logs: list[str] = []

    assert watchdog.mark_stalled(finding, log=logs.append) is None

    assert db.get_run(run["id"])["status"] == "completed"
    assert stall_events(run) == []
    assert any("not running" in line for line in logs)


# ---- what happens to the worker ---------------------------------------


def test_a_daemon_owned_worker_that_acted_is_stopped(home, monkeypatch):
    run = seed(home, age=1200, events=(("tool_call", 1100, {"name": "Edit"}),))
    stopped: list[str] = []
    monkeypatch.setitem(runner.RUNNER.procs, run["id"], object())
    monkeypatch.setattr(
        runner.RUNNER, "stop", lambda rid: stopped.append(rid) or True
    )

    result = watchdog.sweep()

    assert stopped == [run["id"]]
    assert result["stalled"][0]["worker"] == "kill"
    assert result["stalled"][0]["worker_stopped"] is True
    assert stall_events(run)[0]["payload"]["worker"] == "kill"


def test_a_worker_that_never_acted_is_left_to_the_runner_idle_timeout(home, monkeypatch):
    # A silent kill would look like an upstream failure (no tool_call, non-zero
    # exit) and start the route's next endpoint: more spend, not less.
    run = seed(home, age=1200)
    stopped: list[str] = []
    monkeypatch.setitem(runner.RUNNER.procs, run["id"], object())
    monkeypatch.setattr(
        runner.RUNNER, "stop", lambda rid: stopped.append(rid) or True
    )

    result = watchdog.sweep()

    assert stopped == []
    assert result["stalled"][0]["worker"] == "keep-no-tool-call"
    assert db.get_run(run["id"])["status"] == "stalled"


def test_a_worker_owned_by_another_process_is_not_killed(home, monkeypatch):
    run = seed(home, age=1200, events=(("tool_call", 1100, {"name": "Edit"}),))
    stopped: list[str] = []
    monkeypatch.setattr(
        runner.RUNNER, "stop", lambda rid: stopped.append(rid) or True
    )

    result = watchdog.sweep()

    assert stopped == []
    assert result["stalled"][0]["worker"] == "not-owned"


# ---- the board and the light ------------------------------------------


def test_a_stalled_run_is_stopped_not_a_question_or_active_work(home):
    run = seed(home, age=1200)
    watchdog.sweep()

    board = server.board_state()

    assert all(item.get("run") != run["id"] for item in board["needs_you"])
    assert all(item.get("run") != run["id"] for item in board["handling"])
    entry = next(item for item in board["stopped"] if item.get("run") == run["id"])
    assert entry["kind"] == "stopped"
    assert entry["stop_reason"] == "stalled"
    assert entry["failure_kind"] == ""
    assert "无实质进展" in entry["statement"]
    assert "tool_call" in entry["why"]
    assert "不会重试" in entry["plan"]
    # The row is part of the existing stopped length; no fifth counter, and
    # `failed` is still only terminal failures.
    assert board["decision_counts"]["stopped"] == len(board["stopped"])
    assert board["decision_counts"]["failed"] == 0
    assert board["running"] == []


def test_the_pet_light_does_not_call_a_stall_a_failure(home):
    run = seed(home, age=1200)
    watchdog.sweep()

    # `attention` is the status light's one source. A stall is not a spend
    # decision and not a terminal failure, so it does not speak: the board row
    # is the notice. Inventing a third light number would be the S3 bug again.
    assert server.attention() == {"waiting": 0, "running": 0, "failed": 0}
    assert db.get_run(run["id"])["status"] == "stalled"


def test_serve_starts_the_watchdog_unless_turned_off():
    source = inspect.getsource(server._serve_bound)
    assert "watchdog.enabled()" in source
    assert "watchdog.start(log=_log)" in source
    assert "NODUS_WATCHDOG" in inspect.getsource(watchdog.enabled)


# ---- the knobs ---------------------------------------------------------


def test_enabled_reads_the_switch(monkeypatch):
    monkeypatch.delenv("NODUS_WATCHDOG", raising=False)
    assert watchdog.enabled() is True
    monkeypatch.setenv("NODUS_WATCHDOG", "0")
    assert watchdog.enabled() is False
    monkeypatch.setenv("NODUS_WATCHDOG", "1")
    assert watchdog.enabled() is True


def test_stall_seconds_defaults_and_ignores_junk(monkeypatch):
    monkeypatch.delenv("NODUS_STALL_SECONDS", raising=False)
    assert watchdog.stall_seconds() == 600.0
    monkeypatch.setenv("NODUS_STALL_SECONDS", "30")
    assert watchdog.stall_seconds() == 30.0
    monkeypatch.setenv("NODUS_STALL_SECONDS", "not-a-number")
    assert watchdog.stall_seconds() == 600.0
    monkeypatch.setenv("NODUS_STALL_SECONDS", "0")
    assert watchdog.stall_seconds() == 600.0


def test_start_runs_once(home, monkeypatch):
    monkeypatch.setattr(watchdog, "_started", False)
    calls: list[dict] = []
    monkeypatch.setattr(
        watchdog,
        "sweep",
        lambda now=None, log=None: calls.append({"now": now, "log": log})
        or {"running": 0, "stalled": [], "skipped": [], "errors": []},
    )

    # A long interval keeps the background thread asleep for the rest of the
    # suite; the point of this test is the once-only guard and the first pass.
    assert watchdog.start(interval=3600) is True
    assert watchdog.start(interval=3600) is False
    assert len(calls) == 1
