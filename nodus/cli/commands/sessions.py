"""DSH session commands: list, read, hand off, resume."""

from __future__ import annotations

import time

from ... import db, dsh_sessions, runner
from ..client import watch


def add(sub) -> None:
    p = sub.add_parser("sessions", help="list DSH sessions")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--query", default="")
    p.set_defaults(func=sessions)

    p = sub.add_parser("session", help="print a DSH session transcript")
    p.add_argument("id")
    p.add_argument("--tail", type=int, default=40)
    p.add_argument("--reasoning", action="store_true")
    p.set_defaults(func=session)

    p = sub.add_parser("brief", help="write a cross-session handoff")
    p.add_argument("id")
    p.add_argument("--task", required=True)
    p.add_argument("--tail", type=int, default=12)
    p.add_argument("--out", default="")
    p.set_defaults(func=brief)

    p = sub.add_parser("resume", help="append one turn to a DSH session")
    p.add_argument("session")
    p.add_argument("--agent", default="dsh")
    p.add_argument("--note", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=resume, needs_daemon=False)


def sessions(args) -> int:
    rows = dsh_sessions.list_sessions(args.limit, args.query)
    for s in rows:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(s["created_at"]))
        print(
            f"{stamp}  {s['id']}  {s['title'][:44]:<44}  "
            f"user={s['user_messages']:<3} {s['project'][:24]}"
        )
    return 0


def session(args) -> int:
    print(dsh_sessions.transcript(args.id, args.tail, args.reasoning))
    return 0


def brief(args) -> int:
    path = dsh_sessions.write_brief(args.id, args.task, args.out, args.tail)
    print(path)
    return 0


def resume(args) -> int:
    if args.agent != "dsh":
        raise ValueError("only dsh can resume a DSH session")
    meta = dsh_sessions.load(args.session)[0]
    age = time.time() - float(meta.get("updated_at") or 0)
    if age < 120 and not args.force:
        raise RuntimeError(
            f"session was written {int(age)}s ago, another window may own it "
            "(use --force if you are sure it is closed)"
        )
    with dsh_sessions.session_lock(meta["id"]):
        task = db.create_task(
            f"resume {meta['id']}: {args.note}",
            "",
            title=f"resume: {meta['title'][:40]}",
        )
        run = runner.RUNNER.start(
            task["id"], "dsh", cwd=meta["cwd"], note=args.note, session_id=meta["id"]
        )
        print(f"task {task['id']}")
        print(f"run {run['id']} (dsh resume {meta['id']})")
        return watch(run["id"])
