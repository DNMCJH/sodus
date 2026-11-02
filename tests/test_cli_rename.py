"""Phase B/C1: the two entry names, the migration command, and what must not move.

`migrate-home` is the only thing that writes a `~/.sodus`, and only when asked:
dry-run prints a plan, `--apply` copies and verifies, and a failed verification
leaves no half-copied directory. The MCP tool names stay `nodus_*` because
renaming them would break every saved client config.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import pytest

from nodus import config, mcp
from nodus.cli import TOP_LEVEL, build_parser, main
from nodus.cli.commands import migrate


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subcommands registered")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-rename")
    return tmp_path / "home"


def _source_home(tmp_path: Path) -> Path:
    """A believable state directory: one database plus one loose file."""
    source = tmp_path / "old" / ".nodus"
    (source / "tasks").mkdir(parents=True)
    (source / "tasks" / "task-abc").mkdir()
    (source / "tasks" / "task-abc" / "report.md").write_text("done\n", encoding="utf-8")
    (source / "config.json").write_text('{"profile": "default"}', encoding="utf-8")
    conn = sqlite3.connect(source / "nodus.sqlite3")
    try:
        for table in ("task", "run", "artifact"):
            conn.execute(f"CREATE TABLE {table} (id TEXT)")
        conn.execute("INSERT INTO task VALUES ('task-abc')")
        for index in range(3):
            conn.execute("INSERT INTO run VALUES (?)", (f"run-{index}",))
        for index in range(2):
            conn.execute("INSERT INTO artifact VALUES (?)", (f"art-{index}",))
        conn.commit()
    finally:
        conn.close()
    return source


def test_usage_follows_the_name_it_was_called_as():
    assert build_parser("sodus").prog == "sodus"
    assert build_parser("nodus").prog == "nodus"


def test_the_description_names_the_product():
    description = build_parser().description or ""
    assert "Sodus" in description
    assert "nodus" in description  # the implementation name stays stated


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

    assert (target / "tasks" / "task-abc" / "report.md").read_text(encoding="utf-8") == "done\n"
    assert (target / "config.json").read_text(encoding="utf-8") == '{"profile": "default"}'
    conn = sqlite3.connect(target / "nodus.sqlite3")
    try:
        assert conn.execute("SELECT COUNT(*) FROM run").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM artifact").fetchone()[0] == 2
    finally:
        conn.close()
    # One-way: the source is never moved or deleted.
    assert (source / "nodus.sqlite3").is_file()
    assert (source / "config.json").is_file()


def test_apply_refuses_a_non_empty_target(tmp_path, home, capsys):
    source = _source_home(tmp_path)
    target = tmp_path / ".sodus"
    target.mkdir()
    (target / "keep").write_text("mine", encoding="utf-8")

    assert main(["migrate-home", "--from", str(source), "--to", str(target), "--apply"]) == 1
    assert "already exists" in capsys.readouterr().out
    # Nothing was overwritten and nothing was deleted.
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


def test_mcp_tool_names_stay_nodus():
    names = [tool["name"] for tool in mcp.TOOLS]
    assert names, "the MCP server advertises no tools"
    assert all(name.startswith("nodus_") for name in names), names

    reply = mcp.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    listed = [tool["name"] for tool in reply["result"]["tools"]]
    assert listed == names
    # The server identifies as the product name; the tool names stay identifiers.
    assert mcp.SERVER_NAME == "sodus"
