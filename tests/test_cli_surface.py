"""The CLI surface: every advertised command must actually be wired."""

from __future__ import annotations

import argparse

from nodus.cli import build_parser


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subcommands registered")


def test_parser_is_named_nodus():
    parser = build_parser()
    assert parser.prog == "nodus"
    assert "Nodus" in (parser.description or "")


def test_every_advertised_command_has_a_handler():
    # `nodus --help` lists the choices; a command that shows up there but has
    # no handler crashes only when someone types it.
    parser = build_parser()
    subs = _subparsers(parser)
    missing = [name for name, sub in subs.choices.items() if sub.get_default("func") is None]
    assert missing == []
    assert len(subs.choices) >= 29


def test_help_flags_do_not_need_a_daemon():
    # `--help` is the cheapest liveness check we have in CI; it must exit
    # before any HTTP call.
    parser = build_parser()
    for name in _subparsers(parser).choices:
        try:
            parser.parse_args([name, "--help"])
        except SystemExit as exc:  # argparse always exits for --help
            assert exc.code == 0, name
