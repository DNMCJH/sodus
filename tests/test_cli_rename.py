"""migrate-home: the only path from the old ~/.nodus home to ~/.sodus.

Dry-run prints a plan; --apply copies, renames nodus.sqlite3 to sodus.sqlite3,
verifies row counts, and removes its own half-finished target on failure. The
source is never moved or deleted. MCP tool names are all sodus_* now.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from sodus import config, mcp
from sodus.cli import TOP_LEVEL, build_parser, main
from sodus.cli.commands import migrate


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subcommands registered")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("SODUS_WORKSPACE", "ws-rename")
    return tmp_path / "home"


def _source_home(tmp_path: Path) -> Path:
    """A believable old state directory: one sqlite db plus one loose file."""
    source = tmp_path / "old" / ".nodus"
    (source / "tasks").mkdir(parents=True)
    (source / "tasks" / "task-abc").mkdir()
    (source / "tasks" / "task-abc" / "report.md").write_text("done" + chr(10), encoding="utf-8")
    (source / "config.json").write_text('{"profile": "default"}', encoding="utf-8")
    conn = sqlite3.connect(source / "nodus.sqlite3")
    try:
        for table in ("task", "run", "artifact"):
            conn.execute("CREATE TABLE " + table + " (id TEXT)")
        conn.execute("INSERT INTO task VALUES ('task-abc')")
        for index in range(3):
            conn.execute("INSERT INTO run VALUES (?)", ("run-" + str(index),))
        for index in range(2):
            conn.execute("INSERT INTO artifact VALUES (?)", ("art-" + str(index),))
        conn.commit()
    finally:
        conn.close()
    return source


def test_usage_is_sodus():
    assert build_parser().prog == "sodus"
    assert build_parser("sodus").prog == "sodus"


def test_the_description_names_the_product():
    description = build_parser().description or ""
    assert "Sodus" in description


def test_migrate_home_skips_setup_that_would_create_the_target(tmp_path, monkeypatch):
    from sodus import profiles

    called = []
    monkeypatch.setattr(profiles, "ensure_ready", lambda: called.append(True))
    source = _source_home(tmp_path)
    target = tmp_path / "plain" / ".sodus"
    assert main(["migrate-home", "--from", str(source), "--to", str(target)]) == 0
    assert called == []


def test_migrate_home_is_advertised_and_wired():
    assert "migrate-home" in TOP_LEVEL
    subs = _subparsers(build_parser("sodus"))
    assert "migrate-home" in subs.choices
    entry = subs.choices["migrate-home"]
    assert entry.get_default("func") is migrate.migrate_home
    assert entry.get_default("needs_daemon") is False


def test_the_dry_run_writes_nothing(tmp_path, home, capsys):
    source = _source_home(tmp_path)
    target = tmp_path / "new" / ".sodus"
    assert main(["migrate-home", "--from", str(source), "--to", str(target)]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "task=1 run=3 artifact=2" in out
    assert not target.exists()
    assert source.exists()


def test_apply_copies_and_proves_the_copy(tmp_path, home, capsys):
    source = _source_home(tmp_path)
    target = tmp_path / "new" / ".sodus"
    assert main(["migrate-home", "--from", str(source), "--to", str(target), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "done:" in out and "verified" in out

    assert (target / "tasks" / "task-abc" / "report.md").read_text(encoding="utf-8") == "done" + chr(10)
    assert (target / "config.json").read_text(encoding="utf-8") == '{"profile": "default"}'
    conn = sqlite3.connect(target / "sodus.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM artifact").fetchone()[0] == 2
    finally:
        conn.close()
    assert not (target / "nodus.sqlite3").exists()
    # One-way: the source is never moved, renamed or deleted.
    assert (source / "nodus.sqlite3").is_file()
    assert (source / "config.json").is_file()


def test_apply_refuses_a_non_empty_target(tmp_path, home, capsys):
    source = _source_home(tmp_path)
    target = tmp_path / ".sodus"
    target.mkdir()
    (target / "keep").write_text("mine", encoding="utf-8")

    assert main(["migrate-home", "--from", str(source), "--to", str(target), "--apply"]) == 1
    assert "already exists" in capsys.readouterr().out
    assert (target / "keep").read_text(encoding="utf-8") == "mine"


def test_a_failed_verification_leaves_no_half_copy(tmp_path, home, monkeypatch, capsys):
    source = _source_home(tmp_path)
    target = tmp_path / ".sodus"

    def boom(*args, **kwargs):
        raise RuntimeError("simulated mismatch")

    monkeypatch.setattr(migrate, "_verify", boom)
    assert main(["migrate-home", "--from", str(source), "--to", str(target), "--apply"]) == 1
    assert "deleted" in capsys.readouterr().out
    assert not target.exists()


def test_the_missing_source_is_an_error_not_an_empty_migration(tmp_path, home, capsys):
    target = tmp_path / ".sodus"
    rc = main(["migrate-home", "--from", str(tmp_path / "nope"), "--to", str(target)])
    assert rc == 1
    assert "nothing to migrate" in capsys.readouterr().out
    assert not target.exists()


def test_mcp_tools_are_all_sodus():
    names = [tool["name"] for tool in mcp.TOOLS]
    assert names, "the MCP server advertises no tools"
    assert all(name.startswith("sodus_") for name in names)
    assert {"sodus_ask", "sodus_ask_status", "sodus_run"} <= set(names)

    reply = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = [tool["name"] for tool in reply["result"]["tools"]]
    assert listed == names
    assert mcp.SERVER_NAME == "sodus"
