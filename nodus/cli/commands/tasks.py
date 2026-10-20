"""Task and run commands: create, dispatch, follow, inspect."""

from __future__ import annotations

import json

from ..client import call, watch
from .decisions import choice


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
    p.add_argument("--agent", default="claude")
    p.add_argument("--cwd", default=None)
    p.add_argument("--parent", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--model", default="", help="pin the model for this run only")
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
    agent = args.agent
    effort = args.effort
    if agent == "auto":
        from ... import decision

        task_obj = call("GET", f"/tasks/{args.task}")
        ready = [a["name"] for a in call("GET", "/agents") if a["ready"]]
        routed = decision.route_task(task_obj, ready)
        if routed.get("ok"):
            agent = choice(routed, "agent")[0] or "claude"
            effort = effort or (choice(routed, "effort")[0] or "")
            print(
                f"route → {agent} effort={effort or '-'} "
                f"({routed.get('model')}, {routed.get('latency_ms')}ms)"
            )
        else:
            agent = "claude"
            print(f"route unavailable ({routed.get('unavailable')}), fallback claude")
    r = call(
        "POST",
        "/runs",
        {
            "task_id": args.task,
            "agent": agent,
            "cwd": args.cwd,
            "parent_run_id": args.parent,
            "note": args.note,
            "effort": effort,
            "model": args.model,
        },
    )
    print(f"run {r['id']} ({agent})")
    return watch(r["id"])


def runs(args) -> int:
    for r in call("GET", f"/runs?task_id={args.task or ''}"):
        # `completed` alone means the process exited 0. Say whether anything
        # was actually checked, so a blocked worker cannot read as success.
        print(
            f"{r['id']}  {r['status']:<9} {r['agent']:<7} "
            f"verify={r.get('verify', 'none'):<4} task={r['task_id']}"
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
    print(call("POST", f"/runs/{args.run}/stop"))
    return 0
