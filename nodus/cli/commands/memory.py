"""Memory commands: add, recall, list, forget.

These read the database directly. Memory is not a run — there is no agent to
keep alive — so nothing here needs the daemon, and `nodus memory` still works
when `nodus up` is down.
"""

from __future__ import annotations

import time

from ... import memory

SCOPE_CHOICES = ("private", "project", "public")


def add(sub) -> None:
    p = sub.add_parser("memory", help="remembered facts: add, recall, list, forget")
    p.set_defaults(func=memory_help, needs_daemon=False)
    nested = p.add_subparsers(dest="memory_cmd", required=True)

    a = nested.add_parser("add", help="store one fact; same --key overwrites it")
    a.add_argument("text")
    a.add_argument("--scope", default="project", choices=SCOPE_CHOICES)
    a.add_argument("--key", default="", help="stable name, so a later add replaces this fact")
    a.add_argument("--tags", default="", help="comma separated")
    a.add_argument("--agent", default="", help="owner; required for private")
    a.add_argument("--run", default="", help="run id this fact came from")
    a.set_defaults(func=memory_add, needs_daemon=False)

    r = nested.add_parser("recall", help="search visible facts, best match first")
    r.add_argument("query", nargs="?", default="")
    r.add_argument("--scope", default="", choices=("", *SCOPE_CHOICES))
    r.add_argument("--agent", default="", help="whose private facts are visible")
    r.add_argument("--limit", type=int, default=5)
    r.set_defaults(func=memory_recall, needs_daemon=False)

    l = nested.add_parser("list", help="newest visible facts, no query")
    l.add_argument("--scope", default="", choices=("", *SCOPE_CHOICES))
    l.add_argument("--agent", default="")
    l.add_argument("--limit", type=int, default=20)
    l.set_defaults(func=memory_list, needs_daemon=False)

    f = nested.add_parser("forget", help="delete by key or id")
    f.add_argument("target", help="memory key, or a mem-... id")
    f.add_argument("--scope", default="", choices=("", *SCOPE_CHOICES))
    f.add_argument("--agent", default="")
    f.set_defaults(func=memory_forget, needs_daemon=False)


def memory_help(args) -> int:
    print("usage: nodus memory {add,recall,list,forget} ...")
    return 0


def _print(rows: list[dict]) -> int:
    if not rows:
        print("(no memories)")
        return 0
    for row in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(row.get("updated_at") or 0))
        label = row["scope"] + (f"/{row['key']}" if row.get("key") else "")
        print(f"{row['id']}  {label:<22} {when}  {row.get('text') or ''}")
    return 0


def memory_add(args) -> int:
    row = memory.remember(
        args.text,
        scope=args.scope,
        key=args.key,
        tags=args.tags,
        agent=args.agent,
        run_id=args.run,
    )
    label = row["scope"] + (f"/{row['key']}" if row.get("key") else "")
    print(f"{row['id']}  {label}")
    return 0


def memory_recall(args) -> int:
    rows = memory.recall(
        args.query,
        scope=args.scope or None,
        agent=args.agent,
        limit=args.limit,
    )
    return _print(rows)


def memory_list(args) -> int:
    rows = memory.recall("", scope=args.scope or None, agent=args.agent, limit=args.limit)
    return _print(rows)


def memory_forget(args) -> int:
    count = memory.forget(args.target, scope=args.scope, agent=args.agent)
    if not count:
        raise ValueError(f"no memory matched: {args.target}")
    print(f"forgot {count}")
    return 0
