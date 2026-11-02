"""CLI assembly: one parser, one handler per command, no shared globals.

The surface is deliberately small: seventeen top-level names, each one worth
remembering. Everything else still exists, one level down -- the long tail lives
under `nodus debug <name>`, and the four report commands under
`nodus stats <name>`. Old names keep working as hidden aliases, so an existing
script does not break; `nodus --help` just stops printing them.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .. import config

#: What `nodus --help` prints, in the order it prints it.
TOP_LEVEL = (
    # core: the daemon and the health of this machine
    "up",
    "down",
    "status",
    "doctor",
    # work: create, dispatch, follow, inspect
    "task",
    "run",
    "runs",
    "show",
    # decide: proposals waiting for a human
    "proposals",
    "merge",
    "reject",
    # judge: run the tests, review the work
    "verify",
    "review",
    # stats: money, tokens, cache
    "stats",
    # integration
    "mcp",
    "serve",
    # the long tail
    "debug",
)

#: `nodus stats <name>`; also kept as hidden top-level aliases.
STATS_COMMANDS = ("cost", "usage", "bench", "balance")

#: `nodus debug <name>`; also kept as hidden top-level aliases.
DEBUG_COMMANDS = (
    # machine and account
    "agents",
    "probe",
    "decisions",
    "jev",
    "route-for",
    "reconcile",
    "locks",
    "unlock",
    "desk",
    # runs
    "tasks",
    "watch",
    "stop",
    "handoff",
    # pipelines
    "tdd",
    "fix",
    "report",
    "pipelines",
    # sessions
    "sessions",
    "session",
    "brief",
    "lineage",
    "resume",
    "branch",
    # Jev, one call at a time
    "route",
    "triage",
    # configuration
    "providers",
    "routes",
    "profiles",
    "topology",
    # a proposal without the merge
    "proposal",
    # remembered facts
    "memory",
    # harness tributaries
    "observe",
)


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:  # keep Chinese readable in a GBK console
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _prog() -> str:
    """Which name this process was invoked as.

    `nodus` and `sodus` are two console scripts over this one module, so the
    usage line has to follow argv[0] instead of picking a side. A bare
    `python -m nodus` has no console-script name -- argv[0] is `__main__.py`
    -- and stays nodus.
    """
    stem = Path(sys.argv[0]).stem.lower() if sys.argv else ""
    return "sodus" if stem == "sodus" else "nodus"


class _Dropped:
    """Absorbs registration calls for a command a namespace does not carry.

    Command modules register everything they own without knowing the namespace;
    this is what `nodus stats` uses to ignore `serve` while picking up `cost`.
    """

    def add_argument(self, *args, **kwargs):
        return self

    def add_subparsers(self, *args, **kwargs):
        return self

    def add_parser(self, *args, **kwargs):
        return self

    def set_defaults(self, *args, **kwargs):
        return self


class _Surface:
    """One namespace's view of the command modules.

    ``accept`` filters what exists in this namespace, ``show`` filters what its
    help lists. The point is that the argument definitions live in exactly one
    place per command: `nodus usage` stays a hidden alias of `nodus stats
    usage` without a second copy of its flags.
    """

    def __init__(self, action, *, accept=None, show=None):
        self._action = action
        self._accept = accept
        self._show = show

    def add_parser(self, name, **kwargs):
        if self._accept is not None and name not in self._accept:
            return _Dropped()
        if self._show is not None and name not in self._show:
            kwargs["help"] = argparse.SUPPRESS
        return self._action.add_parser(name, **kwargs)

    def __getattr__(self, item):
        return getattr(self._action, item)


def build_parser():
    from .commands import (
        bench,
        decisions,
        memory,
        observe,
        pipeline,
        proposals,
        sessions,
        system,
        tasks,
        topology,
    )

    modules = (
        system, tasks, pipeline, sessions, decisions, topology, proposals,
        bench, memory, observe,
    )

    parser = argparse.ArgumentParser(
        prog=_prog(),
        description="Nodus — task acceptance layer for agents",
    )
    parser.add_argument("--version", action="version", version=f"nodus {config.VERSION}")
    sub = parser.add_subparsers(dest="cmd", metavar="<command>", required=True)

    # The curated top level: every command is registered here, so old names
    # keep parsing, but only TOP_LEVEL is listed.
    for module in modules:
        module.add(_Surface(sub, show=TOP_LEVEL))

    # `nodus stats` shows today; the four reports keep their own flags below it.
    stats = sub.add_parser(
        "stats",
        help="today's spend, tokens, cache hit; cost|usage|bench|balance",
        description="不带子命令 = 今日概览（花费、用量、缓存命中）。",
    )
    stats.set_defaults(func=system.stats_overview, needs_daemon=False)
    stats_sub = stats.add_subparsers(
        dest="stats_cmd", metavar="<cost|usage|bench|balance>"
    )
    for module in (system, bench):
        module.add(_Surface(stats_sub, accept=STATS_COMMANDS))
    stats_order = {name: index for index, name in enumerate(STATS_COMMANDS)}
    stats_sub._choices_actions.sort(key=lambda action: stats_order.get(action.dest, 0))

    # The long tail, listed only where it belongs.
    debug = sub.add_parser(
        "debug",
        help="less common and diagnostic commands (lineage, report, ...)",
    )
    debug_sub = debug.add_subparsers(dest="debug_cmd", metavar="<name>", required=True)
    for module in modules:
        module.add(_Surface(debug_sub, accept=DEBUG_COMMANDS))

    # Modules register in their own order; help should read in TOP_LEVEL order.
    # argparse honours `help=SUPPRESS` for options but not for subparser
    # choices, so the hidden aliases have to be dropped from the listing here.
    sub._choices_actions = [
        action for action in sub._choices_actions if action.help is not argparse.SUPPRESS
    ]
    order = {name: index for index, name in enumerate(TOP_LEVEL)}
    sub._choices_actions.sort(key=lambda action: order.get(action.dest, len(order)))
    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8()
    parser = build_parser()
    args = parser.parse_args(argv)

    # One-time upgrade of a pre-0.4 home. Cheap no-op once done, and it has to
    # happen before any command reads providers or routes.
    from .. import profiles

    try:
        profiles.ensure_ready()
    except OSError as exc:
        print(f"warning: could not finish setup: {exc}", file=sys.stderr)

    if getattr(args, "needs_daemon", True):
        from .client import ensure_daemon

        try:
            ensure_daemon()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    try:
        return int(args.func(args) or 0)
    except (ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
