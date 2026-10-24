"""The CLI surface: every advertised command must actually be wired.

The surface is curated on purpose: a user should be able to read `nodus
--help` in one screen. Everything that left the top level still has to work
as a hidden alias, because scripts and muscle memory outlive help text.
"""

from __future__ import annotations

import argparse
import re

import pytest

from nodus import config
from nodus.cli import (
    DEBUG_COMMANDS,
    STATS_COMMANDS,
    TOP_LEVEL,
    build_parser,
    main,
)
from nodus.cli.commands import system


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subcommands registered")


def _listed_in_help(text: str) -> list[str]:
    """Command names as the formatter lays them out: four spaces, then the name."""
    return re.findall(r"^    (\S+)", text, re.MULTILINE)


def _help(capsys, argv: list[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(argv)
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_parser_is_named_nodus():
    parser = build_parser()
    assert parser.prog == "nodus"
    assert "Nodus" in (parser.description or "")


def test_every_advertised_command_has_a_handler():
    # A command that shows up in `--help` but has no handler crashes only when
    # someone types it. `debug` is a namespace with required children; its
    # children are checked in the debug test below.
    parser = build_parser()
    subs = _subparsers(parser)
    missing = [
        name
        for name, sub in subs.choices.items()
        if name != "debug" and sub.get_default("func") is None
    ]
    assert missing == []
    # Nothing was deleted, only moved: the old fifty plus stats and debug.
    assert set(subs.choices) == set(TOP_LEVEL) | set(STATS_COMMANDS) | set(DEBUG_COMMANDS)


def test_help_flags_do_not_need_a_daemon():
    # `--help` is the cheapest liveness check we have in CI; it must exit
    # before any HTTP call.
    parser = build_parser()
    for name in _subparsers(parser).choices:
        try:
            parser.parse_args([name, "--help"])
        except SystemExit as exc:  # argparse always exits for --help
            assert exc.code == 0, name


def test_top_level_help_lists_exactly_the_curated_names(capsys):
    text = _help(capsys, ["--help"])
    assert _listed_in_help(text) == list(TOP_LEVEL)
    assert len(TOP_LEVEL) == 17
    assert len(text.splitlines()) <= 30
    for namespace in ("lineage", "reconcile", "triage", "handoff"):
        assert f"    {namespace} " not in text


def test_hidden_aliases_still_parse_and_share_one_handler():
    parser = build_parser()
    subs = _subparsers(parser)
    hidden = [*STATS_COMMANDS, *DEBUG_COMMANDS]
    assert sorted(hidden) == sorted(set(hidden))
    assert all(name in subs.choices for name in hidden)
    assert not set(hidden) & set(TOP_LEVEL)

    # `usage` is one definition with two entry points: stats usage and the old
    # top-level alias must land on the same handler, flags and all.
    stats_subs = _subparsers(subs.choices["stats"])
    for name in STATS_COMMANDS:
        handler = stats_subs.choices[name].get_default("func")
        assert handler is not None
        assert subs.choices[name].get_default("func") is handler
    alias = parser.parse_args(["usage", "--period", "day"])
    assert alias.func is stats_subs.choices["usage"].get_default("func")
    assert alias.period == "day"


def test_stats_defaults_to_todays_overview():
    args = build_parser().parse_args(["stats"])
    assert args.func is system.stats_overview
    assert args.needs_daemon is False


def test_debug_lists_the_long_tail_and_every_entry_runs(capsys):
    parser = build_parser()
    debug_subs = _subparsers(_subparsers(parser).choices["debug"])
    assert set(debug_subs.choices) == set(DEBUG_COMMANDS)
    assert all(sub.get_default("func") is not None for sub in debug_subs.choices.values())

    text = _help(capsys, ["debug", "--help"])
    assert set(_listed_in_help(text)) == set(DEBUG_COMMANDS)
    for name in (
        "lineage",
        "report",
        "desk",
        "topology",
        "probe",
        "reconcile",
        "triage",
        "locks",
        "handoff",
    ):
        assert name in text
    # Every entry answers --help from inside the namespace, so none of them
    # fell out while the tree was being assembled.
    for name in DEBUG_COMMANDS:
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["debug", name, "--help"])
        assert exc.value.code == 0, name
    # A top-level command is not a debug command.
    assert "    run " not in text


def test_a_debug_entry_point_runs_for_real(tmp_path, monkeypatch, capsys):
    # Namespace parsing is cheap to assert; this one goes through `main` so a
    # broken needs_daemon or dispatch shows up here rather than at the user.
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.setenv("NODUS_WORKSPACE", "ws-cli")
    assert main(["debug", "locks"]) == 0
    assert "locked" in capsys.readouterr().out
