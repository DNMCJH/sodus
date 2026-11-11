"""`/board` may gain fields; the ones already consumed must keep their shape.

MCP (`sodus/mcp.py`) and the desktop pet (`sodus-status.js`) read `today`,
`running` and `decisions`. `unverified_runs` is additive, and it exists so the
page can name the runs behind a count instead of offering one blind rerun.
"""

from __future__ import annotations

from sodus import board as server


def _run(rid: str, **over) -> dict:
    row = {
        "id": rid,
        "task_id": "task-a",
        "agent": "claude",
        "route": "claude",
        "status": "completed",
        "note": "只写这一行",
        "started_at": 100.0,
        "ended_at": 200.0,
        "cwd": "C:\\tmp\\demo",
        "error": "",
    }
    row.update(over)
    return row


def test_unverified_rows_are_one_per_run_newest_first():
    runs = [
        _run("run-old", ended_at=100.0),
        _run("run-new", task_id="task-b", ended_at=300.0),
        _run("run-red", ended_at=250.0),
        _run("run-failed", status="failed", ended_at=400.0),
    ]
    rows = server._unverified_rows(
        runs, {"run-red": "red"}, {"task-a": "写一个文件", "task-b": "再写一个"}
    )
    assert [row["id"] for row in rows] == ["run-new", "run-old"]
    assert rows[0]["title"] == "再写一个"
    assert rows[0]["task_id"] == "task-b"
    assert rows[0]["verify"] == "none"
    assert set(rows[0]) == {"id", "task_id", "title", "agent", "ended_at", "verify"}


def test_unverified_rows_fall_back_to_the_note_and_stop_at_fifty():
    runs = [_run(f"run-{i}", ended_at=float(i)) for i in range(60)]
    rows = server._unverified_rows(runs, {}, {})
    assert len(rows) == 50
    assert rows[0]["ended_at"] == 59.0
    assert rows[0]["title"] == "只写这一行"


class _EmptyDB:
    """Just enough connection for the live/today reads in board_state."""

    def execute(self, *args, **kwargs):
        return self

    def fetchall(self):
        return []

    def __iter__(self):
        return iter(())

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def fetchone(self):
        return None

    def close(self):
        pass


def test_board_keeps_its_old_keys_and_adds_unverified_runs(monkeypatch):
    # board_state asks for the work kind: the 50-row window must not be
    # filled by pet chatter (test_run_kind.py covers the filter itself).
    monkeypatch.setattr(
        server.db, "list_runs", lambda limit=50, kind=None: [_run("run-a")]
    )
    monkeypatch.setattr(server.db, "verify_states", lambda ids: {"run-a": "none"})
    monkeypatch.setattr(server.db, "list_proposals", lambda status: [])
    monkeypatch.setattr(server, "_task_titles", lambda ids: {"task-a": "写一个文件"})
    monkeypatch.setattr(server.db, "connect", lambda: _EmptyDB())

    board = server.board_state(now=1_700_000_000.0)
    assert set(board) == {"now", "day", "profile", "running", "decisions", "decision_counts",
                          "unverified_runs", "needs_you", "handling", "stopped", "unverifiable",
                          "auto_recovering", "deterministic", "history_failures", "memory_candidates",
                          "terminals", "today"}
    assert {
        "now",
        "day",
        "profile",
        "running",
        "decisions",
        "decision_counts",
        "today",
    } <= set(board)
    assert board["decision_counts"]["unverified"] == 1
    assert board["unverified_runs"] == [
        {
            "id": "run-a",
            "task_id": "task-a",
            "title": "写一个文件",
            "agent": "claude",
            "ended_at": 200.0,
            "verify": "none",
        }
    ]
