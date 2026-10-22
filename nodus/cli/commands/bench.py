"""`nodus bench`: score one route on the task set in `eval/tasks`.

Isolation decision: the benchmark writes no state into the real Nodus home.
`config.HOME` is a module attribute read at call time by `db.connect`,
`config.ensure_dirs`, `profiles.*` and the runner's run directory, so pointing
it at a scratch directory before the first task is enough to isolate the whole
run — no monkeypatching of `db`, no second process, no new dependency.

Secrets are the one thing a scratch home cannot supply. They are read once
from the real home and handed to the runner in memory (`config.load_secrets`
is redirected for the duration), never copied into `%TEMP%`: a benchmark is
not a reason to put an API key on a second disk.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ... import bench, config, routes


def add(sub) -> None:
    p = sub.add_parser("bench", help="score a route on the eval/tasks task set")
    p.add_argument("--route", default="opencode-glm", help="route name (default: opencode-glm)")
    p.add_argument("--tasks", default="eval/tasks", help="task directory (default: eval/tasks)")
    p.add_argument("--repeat", type=int, default=1, help="run every task this many times")
    p.add_argument("--only", default="", help="comma-separated task ids")
    p.add_argument("--out", default="", help="write results.json here (default: scratch dir)")
    p.add_argument("--keep", action="store_true", help="keep the scratch work trees and home")
    p.set_defaults(func=bench_cmd, needs_daemon=False)


def _copy_settings(target: Path) -> None:
    """Routes and providers are configuration, not state: a benchmark should
    see the same route table the operator does."""
    for name in ("config.json", "providers.json", "routes.json"):
        source = config.HOME / name
        if source.is_file():
            shutil.copy2(source, target / name)


def bench_cmd(args) -> int:
    tasks_dir = Path(args.tasks).expanduser().resolve()
    try:
        tasks = bench.discover(tasks_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.only:
        wanted = [part.strip() for part in args.only.split(",") if part.strip()]
        known = {task.id for task in tasks}
        unknown = [name for name in wanted if name not in known]
        if unknown:
            print(f"error: unknown task(s): {', '.join(unknown)}", file=sys.stderr)
            return 1
        tasks = [task for task in tasks if task.id in wanted]
    if not tasks:
        print("error: no tasks selected", file=sys.stderr)
        return 1

    try:
        routes.resolve_for_run(args.route)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    secrets = config.load_secrets()
    if not routes.ready(args.route, secrets):
        print(f"error: route {args.route} is not ready on this machine", file=sys.stderr)
        return 1

    base = bench.scratch_dir()
    home = base / "home"
    home.mkdir(parents=True)
    _copy_settings(home)

    real_home = config.HOME
    real_load_secrets = config.load_secrets
    config.HOME = home
    config.load_secrets = lambda ws=None: dict(secrets)  # noqa: ARG005 - keep the signature
    try:
        payload = bench.run(
            tasks,
            args.route,
            repeat=args.repeat,
            base_dir=base,
            keep=args.keep,
            out=Path(args.out).expanduser() if args.out else None,
            progress=lambda message: print(message, flush=True),
        )
    finally:
        config.load_secrets = real_load_secrets
        config.HOME = real_home
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)

    print()
    print(payload["report"])
    print(f"\nresults: {payload['out']}")
    if args.keep:
        print(f"work dirs: {base / 'work'}")
    return 0
