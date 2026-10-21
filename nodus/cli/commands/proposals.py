"""Proposal commands: list, inspect, merge, reject.

Merging is a git operation plus the main tree's write lock, so these run in the
CLI process rather than through the daemon: there is no agent to keep alive.
"""

from __future__ import annotations

import time

from ... import db, propose


def add(sub) -> None:
    p = sub.add_parser("proposals", help="open proposals (work on a branch, not merged)")
    p.add_argument("--all", action="store_true", help="include merged and rejected ones")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=proposals, needs_daemon=False)

    p = sub.add_parser("proposal", help="show one proposal and its patch")
    p.add_argument("id")
    p.add_argument("--lines", type=int, default=120)
    p.set_defaults(func=proposal, needs_daemon=False)

    p = sub.add_parser("merge", help="apply a proposal to the main tree")
    p.add_argument("id")
    p.set_defaults(func=merge, needs_daemon=False)

    p = sub.add_parser("reject", help="drop a proposal and its worktree")
    p.add_argument("id")
    p.set_defaults(func=reject, needs_daemon=False)


def _short(pid: str) -> str:
    return pid


def proposals(args) -> int:
    rows = db.list_proposals("" if args.all else "open", args.limit)
    if not rows:
        print("(no proposals)")
        return 0
    for row in rows:
        age = time.strftime("%m-%d %H:%M", time.localtime(row.get("created_at") or 0))
        print(
            f"{row['id']:<22} {row['status']:<9} {age}  {row.get('stat') or '-':<22} "
            f"{(row.get('branch') or '')[:28]}"
        )
    return 0


def proposal(args) -> int:
    row = db.get_proposal(args.id)
    if not row:
        raise ValueError(f"unknown proposal: {args.id}")
    print(f"# {row['id']}  {row['status']}")
    print(f"- 仓库：{row['repo']}")
    print(f"- 分支：{row['branch']}（基于 {row['base'][:8]}）")
    print(f"- 工作区：{row['worktree']}")
    print(f"- 改动：{row.get('stat')}")
    print(f"- run：{row['run_id']}")
    print("- 文件：")
    for item in row.get("files") or []:
        print(f"    {item['path']}  +{item['added']} −{item['removed']}")
    print("\n## patch\n")
    print(propose.preview(row, args.lines))
    if row["status"] == "open":
        print(f"\n合入：nodus merge {row['id']}    丢弃：nodus reject {row['id']}")
    return 0


def merge(args) -> int:
    row = db.get_proposal(args.id)
    if not row:
        raise ValueError(f"unknown proposal: {args.id}")
    if row["status"] != "open":
        raise ValueError(f"proposal is {row['status']}, not open")
    result = propose.merge(row)
    if not result["ok"]:
        print(f"merge failed: {result['reason']}")
        return 2
    print(f"merged {row['branch']} into {row['repo']} at {result['commit']}")
    print(f"proposal {row['id']} closed; worktree and branch removed")
    return 0


def reject(args) -> int:
    row = db.get_proposal(args.id)
    if not row:
        raise ValueError(f"unknown proposal: {args.id}")
    if row["status"] != "open":
        raise ValueError(f"proposal is {row['status']}, not open")
    propose.reject(row)
    print(f"rejected {row['id']}; worktree and branch removed")
    return 0
