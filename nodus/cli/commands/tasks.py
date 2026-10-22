"""Task and run commands: create, dispatch, follow, inspect."""

from __future__ import annotations

import json

from ..client import call, watch


def add(sub) -> None:
    p = sub.add_parser("task", help="create a task")
    p.add_argument("goal")
    p.add_argument("--accept", default="")
    p.add_argument("--title", default="")
    p.set_defaults(func=task)

    p = sub.add_parser("tasks", help="list tasks")
    p.set_defaults(func=tasks)

    p = sub.add_parser("run", help="start a run for a task")
    p.add_argument("task")
    p.add_argument("--agent", default="", help="route name (kept for older scripts)")
    p.add_argument("--route", default="", help="route: agent + ordered fallback chain")
    p.add_argument("--profile", default="", help="profile to run under")
    p.add_argument("--cwd", default=None)
    p.add_argument("--parent", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--model", default="", help="pin the model for this run only")
    p.add_argument(
        "--propose",
        action="store_true",
        help="work in an isolated git worktree and produce a mergeable proposal",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="do not run the project's tests when the worker changed files",
    )
    p.add_argument(
        "--no-memory",
        action="store_true",
        help="do not inject remembered facts into the brief",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=0.0,
        help="seconds to wait for a busy --cwd instead of failing at once",
    )
    p.set_defaults(func=run)

    p = sub.add_parser("watch", help="follow a run")
    p.add_argument("run")
    p.add_argument("--after", type=int, default=0)
    p.set_defaults(func=lambda a: watch(a.run, a.after))

    p = sub.add_parser("runs", help="list runs")
    p.add_argument("--task", default=None)
    p.set_defaults(func=runs)

    p = sub.add_parser("show", help="raw run record as JSON")
    p.add_argument("run")
    p.set_defaults(func=show)

    p = sub.add_parser("handoff", help="print the handoff a run produced")
    p.add_argument("run")
    p.set_defaults(func=handoff)

    p = sub.add_parser("stop", help="kill a run's process tree")
    p.add_argument("run")
    p.set_defaults(func=stop)


def task(args) -> int:
    t = call(
        "POST",
        "/tasks",
        {"goal": args.goal, "acceptance": args.accept, "title": args.title},
    )
    print(t["id"])
    return 0


def tasks(args) -> int:
    for t in call("GET", "/tasks"):
        print(f"{t['id']}  {t['status']:<10} {t['title']}")
    return 0


def run(args) -> int:
    # `auto` is not resolved here: the daemon owns the policy layer, and doing
    # it client-side would mean two dispatchers disagreeing about the same
    # task. The run row that comes back says which route actually ran.
    route = args.route or args.agent or "claude"
    r = call(
        "POST",
        "/runs",
        {
            "task_id": args.task,
            "route": route,
            "profile": args.profile,
            "cwd": args.cwd,
            "parent_run_id": args.parent,
            "note": args.note,
            "effort": args.effort,
            "model": args.model,
            "wait": args.wait,
            "verify": not args.no_verify,
            "propose": args.propose,
            "no_memory": args.no_memory,
        },
    )
    print(f"run {r['id']} ({r.get('route') or route})")
    return watch(r["id"])


def runs(args) -> int:
    for r in call("GET", f"/runs?task_id={args.task or ''}"):
        # `completed` alone means the process exited 0. Say whether anything
        # was actually checked, so a blocked worker cannot read as success.
        print(
            f"{r['id']}  {r['status']:<9} {(r.get('route') or r['agent']):<8} "
            f"{(r.get('provider') or '-'):<12} verify={r.get('verify', 'none'):<4} "
            f"task={r['task_id']}"
        )
    return 0


def show(args) -> int:
    print(json.dumps(call("GET", f"/runs/{args.run}"), indent=2, ensure_ascii=False))
    return 0


def handoff(args) -> int:
    h = call("GET", f"/runs/{args.run}/handoff")
    print(h.get("text") or "(no handoff)")
    return 0


def stop(args) -> int:
    if args.run.startswith("pipe-"):
        print(call("POST", f"/pipelines/{args.run}/stop"))
        return 0
    print(call("POST", f"/runs/{args.run}/stop"))
    return 0
