"""Task and run commands: create, dispatch, follow, inspect."""

from __future__ import annotations

import json
import os
import sys

from ..client import call, watch


def add(sub) -> None:
    p = sub.add_parser("task", help="create a task")
    p.add_argument("goal")
    p.add_argument("--accept", default="")
    p.add_argument("--title", default="")
    p.set_defaults(func=task)

    p = sub.add_parser("tasks", help="list tasks")
    p.set_defaults(func=tasks)

    p = sub.add_parser("run", help="start (or resume) a run for a task")
    p.add_argument("task", nargs="?")
    p.add_argument(
        "--continue",
        dest="continue_run",
        default="",
        metavar="RUN",
        help="resume the vendor session of a previous run (same conversation)",
    )
    p.add_argument(
        "--resume",
        dest="resume_session",
        default="",
        metavar="SESSION",
        help="resume a vendor session id (needs --route/--agent)",
    )
    p.add_argument("--agent", default="", help="route name (kept for older scripts)")
    p.add_argument("--route", default="", help="route: agent + ordered fallback chain")
    p.add_argument("--profile", default="", help="profile to run under")
    p.add_argument("--cwd", default=None)
    p.add_argument("--parent", default=None)
    p.add_argument("--note", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--model", default="", help="pin the model for this run only")
    p.add_argument(
        "--allow-expensive",
        action="store_true",
        help="run an expensive route even after today's soft budget is used up",
    )
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
    p.add_argument(
        "--review",
        action="store_true",
        help="force coordinator review even for explicit routes",
    )
    p.add_argument(
        "--no-review",
        action="store_true",
        help="skip review entirely, even on auto routes",
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
    route = args.route or args.agent or ""
    body: dict = {
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
        "allow_expensive": args.allow_expensive,
        "force_review": args.review,
        "skip_review": args.no_review,
    }

    if args.continue_run:
        # Same conversation, new turn: the previous run row is the only place
        # the vendor session id is recorded, so a killed run that persisted it
        # early (runner does) can be continued here without redoing the work.
        previous = call("GET", f"/runs/{args.continue_run}")
        try:
            ref = json.loads(previous.get("session_ref_json") or "{}")
        except ValueError:
            ref = {}
        session_id = str(ref.get("session_id") or "")
        if not session_id:
            print(
                f"error: {args.continue_run} 没有可续的会话（session_ref 为空）。"
                "被强杀/孤儿化的 run 只有早期落盘的 session_id 才能续；"
                "也可以先用 sodus debug sessions 找到会话再 --resume。",
                file=sys.stderr,
            )
            return 1
        body.update(
            {
                "task_id": previous["task_id"],
                "route": route
                or previous.get("route")
                or previous.get("agent")
                or "",
                "profile": args.profile or previous.get("profile") or "",
                "cwd": args.cwd or previous.get("cwd"),
                "parent_run_id": previous["id"],
                "note": args.note or f"继续 {previous['id']} 的会话",
                "effort": args.effort or previous.get("effort") or "",
                "model": args.model or previous.get("model") or "",
                "session_id": session_id,
                "session_mode": "resume",
            }
        )
        print(f"继续 {previous['id']} 的会话 {session_id}")
    elif args.resume_session:
        if not route:
            print(
                "error: --resume 需要 --route/--agent 指定用哪个 harness 接续",
                file=sys.stderr,
            )
            return 1
        task_id = args.task
        if not task_id:
            task = call(
                "POST",
                "/tasks",
                {"goal": args.note or "接续会话", "acceptance": ""},
            )
            task_id = task["id"]
        body.update(
            {
                "task_id": task_id,
                "route": route,
                "cwd": args.cwd or os.getcwd(),
                "session_id": args.resume_session,
                "session_mode": "resume",
            }
        )
        print(f"接续会话 {args.resume_session}（{route}）")
    else:
        if not args.task:
            print(
                "error: 要给一个 task id；接续旧会话用 --continue <run> 或 --resume <session>",
                file=sys.stderr,
            )
            return 1
        body["task_id"] = args.task
        # Empty means "whatever the daemon's configured default is": the
        # operator sets it in config.routing.default, and duplicating that
        # decision here is how the two silently disagreed about "claude".
        body["route"] = route

    r = call("POST", "/runs", body)
    print(f"run {r['id']} ({r.get('route') or body.get('route') or ''})")
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
    # Interrupting is not losing the conversation: the runner persisted the
    # vendor session id before the kill, so say how to pick it back up.
    try:
        run = call("GET", f"/runs/{args.run}")
        ref = json.loads(run.get("session_ref_json") or "{}")
    except (RuntimeError, ValueError):
        ref = {}
    if ref.get("session_id"):
        print(
            f"会话已保留（{ref.get('provider') or 'vendor'}:{ref['session_id']}）；"
            f"接着聊：sodus run --continue {args.run}"
        )
    return 0
