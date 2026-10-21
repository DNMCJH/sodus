"""CLI assembly: one parser, one handler per command, no shared globals."""

from __future__ import annotations

import sys

from .. import config


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:  # keep Chinese readable in a GBK console
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def build_parser():
    import argparse

    from .commands import decisions, pipeline, proposals, sessions, system, tasks, topology

    parser = argparse.ArgumentParser(
        prog="nodus", description="Nodus — task acceptance layer for agents"
    )
    parser.add_argument("--version", action="version", version=f"nodus {config.VERSION}")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for module in (system, tasks, pipeline, sessions, decisions, topology, proposals):
        module.add(sub)
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
