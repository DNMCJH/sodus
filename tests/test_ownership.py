"""A daemon must not adopt runs it did not start.

`sodus mcp` and `sodus run` hold their own runs. Before ownership was recorded,
starting the daemon failed all of them and reaped their files — silent loss for
anyone who used two processes.
"""

from __future__ import annotations

import os
import socket

from sodus import db, runner


def run_row(**over):
    row = {"id": "run-x", "status": "running", "owner_pid": os.getpid(), "owner_host": socket.gethostname()}
    row.update(over)
    return row


def test_own_live_run_is_not_an_orphan():
    assert runner.Runner._owner_is_gone(run_row()) is False


def test_dead_owner_is_an_orphan():
    # 999999 is above the pid ceiling on Windows and Linux alike.
    assert runner.Runner._owner_is_gone(run_row(owner_pid=999999)) is True


def test_another_host_is_left_alone():
    # A foreign pid cannot be checked, and a wrong guess kills real work.
    assert runner.Runner._owner_is_gone(run_row(owner_host="other-machine")) is False


def test_row_without_owner_keeps_the_old_rule():
    # Rows that predate owner recording get the historical behaviour.
    assert runner.Runner._owner_is_gone(run_row(owner_pid=None)) is True


def test_create_run_records_its_owner():
    created = db.create_run(task_id="own-test", agent="claude", cwd=os.getcwd())
    try:
        row = db.get_run(created["id"])
        assert row["owner_pid"] == os.getpid()
        assert row["owner_host"] == socket.gethostname()
    finally:
        conn = db.connect()
        conn.execute("DELETE FROM run WHERE id=?", (created["id"],))
        conn.commit()
        conn.close()
