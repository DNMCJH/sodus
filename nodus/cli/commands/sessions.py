"""Session commands: list, read, hand off, continue, branch, trace lineage.

Five verbs over three vendors. The point of this module is that nobody has to
remember which CLI wrote a conversation before acting on it.
"""

from __future__ import annotations

import json
import time

from ... import lineage, sessions
from ..client import call, watch


def add(sub) -> None:
    p = sub.add_parser("sessions", help="list sessions from every vendor")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--query", default="", help="substring of title, cwd or text")
    p.add_argument("--vendor", default="", choices=["", "claude", "codex", "dsh"])
    p.add_argument("--source", default="", choices=["", "run", "user"],
                   help="run = produced by a Nodus run, user = your own")
    p.add_argument("--no-orphans", action="store_true", help="hide runs whose session file is gone")
    p.set_defaults(func=sessions_cmd, needs_daemon=False)

    p = sub.add_parser("session", help="print one session transcript")
    p.add_argument("id")
    p.add_argument("--tail", type=int, default=40)
    p.add_argument("--reasoning", action="store_true")
    p.set_defaults(func=session, needs_daemon=False)

    p = sub.add_parser("brief", help="write a cross-session handoff")
    p.add_argument("id")
    p.add_argument("--task", required=True)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--out", default="")
    p.set_defaults(func=brief, needs_daemon=False)

    p = sub.add_parser("lineage", help="where a session came from and what it produced")
    p.add_argument("id", help="a ses- / run- / task- id, or a vendor session id")
    p.add_argument("--json", action="store_true", help="the structured record")
    p.set_defaults(func=lineage_cmd, needs_daemon=False)

    p = sub.add_parser("resume", help="continue a session (any vendor)")
    p.add_argument("session")
    p.add_argument("--note", required=True)
    p.add_argument("--route", default="", help="override which endpoint takes the turn")
    p.add_argument("--profile", default="")
    p.add_argument("--force", action="store_true")
    p.add_argument(
        "--in-place",
        action="store_true",
        help="append to the session even if it is one of yours (default: branch instead)",
    )
    p.set_defaults(func=resume)

    p = sub.add_parser("branch", help="start a new session from this one's history")
    p.add_argument("session")
    p.add_argument("--note", default="", help="the first instruction of the new session")
    p.add_argument("--route", default="")
    p.add_argument("--profile", default="")
    p.add_argument(
        "--into",
        default="",
        choices=["", "claude", "codex", "dsh"],
        help="hand the conversation to another vendor as a seeded new session",
    )
    p.set_defaults(func=branch)


def _age(ts: float) -> str:
    if not ts:
        return "?"
    delta = time.time() - ts
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m"
    if delta < 86400:
        return f"{int(delta / 3600)}h"
    return f"{int(delta / 86400)}d"


def sessions_cmd(args) -> int:
    rows = sessions.list_all(
        query=args.query,
        limit=args.limit,
        vendor=args.vendor,
        source=args.source,
        include_orphans=not args.no_orphans,
    )
    if not rows:
        print("(no sessions match)")
        return 0
    for session in rows:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(session.updated_at))
        tag = session.run_id[:14] if session.run_id else "user"
        print(
            f"{stamp}  {session.vendor + ':' + session.id[:12]:<22} "
            f"{session.origin:<22} {tag:<15} {session.title[:44]}"
        )
    return 0


def session(args) -> int:
    print(sessions.transcript(args.id, args.tail, args.reasoning))
    return 0


def brief(args) -> int:
    path = sessions.write_brief(args.id, args.task, args.out, args.tail)
    print(path)
    return 0


def lineage_cmd(args) -> int:
    data = lineage.report(args.id)
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    else:
        print(lineage.render(data))
    return 0


def _too_fresh(session, force: bool) -> None:
    """A session written seconds ago is probably still open in another window."""
    age = time.time() - float(session.updated_at or 0)
    if age < 120 and not force:
        raise RuntimeError(
            f"session was written {int(age)}s ago, another window may own it "
            "(use --force if you are sure it is closed)"
        )


def _task_for(kind: str, session, note: str) -> dict:
    title = f"{kind}: {(session.title or session.id)[:60]}"
    goal = f"{kind} {session.vendor}:{session.id}"
    if note:
        goal += f" — {note[:120]}"
    return call("POST", "/tasks", {"goal": goal, "acceptance": "", "title": title})


def resume(args) -> int:
    plan = sessions.plan(
        args.session, fork=False, route=args.route, profile=args.profile, in_place=args.in_place
    )
    session = plan["session"]
    if plan["mode"] != "resume":
        # A session the user wrote is not continued in place by default: it is
        # the same store their own CLI reads, so a turn appended here shows up
        # in a conversation they are still having. Say so, then branch.
        print(plan["note"] or "从这段历史分叉，不写回原会话")
        return _branch(plan, args, kind="resume")
    _too_fresh(session, args.force)
    print(f"接续 {session.vendor}:{session.id}（原地追加一轮）")
    print("  发送的内容就是你给的 --note，原文：")
    for line in args.note.splitlines():
        print(f"    | {line}")
    print(f"  它会出现在：nodus session {session.id}")
    if session.vendor == "dsh":
        print("  以及你自己的 DSH GUI（同一个 sessions 存储）")
    route = args.route or session.vendor
    with sessions.session_lock(session.id):
        task = _task_for("resume", session, args.note)
        run = call(
            "POST",
            "/runs",
            {
                "task_id": task["id"],
                "route": route,
                "provider": "" if args.route else plan["provider"],
                "profile": plan["profile"],
                "cwd": session.cwd or None,
                "note": args.note,
                "session_id": plan["session_id"],
                "session_mode": "resume",
            },
        )
        print(f"task {task['id']}")
        print(f"run {run['id']} ({session.vendor} resume {session.id})")
        return watch(run["id"])


def _branch(plan: dict, args, kind: str = "branch") -> int:
    session = plan["session"]
    note = args.note
    if plan["mode"] == "seed":
        # DSH has no native fork. A seeded session starts from a written
        # handoff, which is a different thing from replaying the history.
        note = sessions.brief(
            f"{session.vendor}:{session.id}",
            note or f"从 {session.id} 分叉，继续下去",
            tail=12,
        )
        provider = ""
        session_id = ""
        session_mode = "resume"
    else:
        provider = "" if args.route else plan["provider"]
        session_id = plan["session_id"]
        session_mode = "fork"
        _too_fresh(session, force=True)  # a fork reads, it does not write
    route = args.route or plan.get("agent") or session.vendor
    task = _task_for(kind, session, note)
    run = call(
        "POST",
        "/runs",
        {
            "task_id": task["id"],
            "route": route,
            "provider": provider,
            "profile": plan["profile"],
            "cwd": session.cwd or None,
            "note": note,
            "session_id": session_id,
            "session_mode": session_mode,
        },
    )
    print(f"task {task['id']}")
    print(f"run {run['id']} ({session.vendor} {plan['mode']} {session.id})")
    return watch(run["id"])


def branch(args) -> int:
    plan = sessions.plan(
        args.session, fork=True, route=args.route, profile=args.profile, into=args.into
    )
    print(plan["note"])
    return _branch(plan, args)
