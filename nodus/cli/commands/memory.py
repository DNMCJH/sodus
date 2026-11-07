"""`nodus debug memory`: what the harnesses remembered, and what to adopt.

Memory is not a Nodus table. It is the plain text each harness already keeps —
`CLAUDE.md`, `AGENTS.md` — and the one thing missing is a summary a human can
review. These commands read those files directly: no daemon, no database, and
`nodus debug memory` keeps working when `nodus up` is down.
"""

from __future__ import annotations

import json

from ... import config, memory


def add(sub) -> None:
    p = sub.add_parser("memory", help="harvest harness memory files: sources, harvest, adopt")
    p.set_defaults(func=memory_help, needs_daemon=False)
    nested = p.add_subparsers(dest="memory_cmd", metavar="<sources|harvest|adopt>", required=True)

    d = nested.add_parser("dream", help="consolidate episodes into staged candidates")
    mode = d.add_mutually_exclusive_group(required=True)
    mode.add_argument("--now", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    d.add_argument("--limit", type=int, default=50)
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=memory_dream, needs_daemon=False)

    r = nested.add_parser("reindex", help="refresh the marker hash after manual edits")
    r.add_argument("--project", default="")
    r.set_defaults(func=memory_reindex, needs_daemon=False)
    rb = nested.add_parser("rollback", help="rollback memory to a recorded version")
    rb.add_argument("--to", type=int, required=True)
    rb.add_argument("--project", default="")
    rb.set_defaults(func=memory_rollback, needs_daemon=False)

    s = nested.add_parser("sources", help="which memory files were found, and how big")
    s.add_argument("--project", default="", help="project directory; default is cwd")
    s.set_defaults(func=memory_sources, needs_daemon=False)

    h = nested.add_parser("harvest", help="print the facts read out of those files")
    h.add_argument("--project", default="")
    h.add_argument("--json", action="store_true", help="machine readable: ids, sources, conflicts")
    h.add_argument("--all", action="store_true", help="include facts already adopted")
    h.add_argument(
        "--limit", type=int, default=40, help="how many to print; 0 prints every one"
    )
    h.set_defaults(func=memory_harvest, needs_daemon=False)

    a = nested.add_parser("adopt", help="append candidates to AGENTS.md ## 已验证事实")
    a.add_argument("ids", nargs="+", help="candidate ids from `debug memory harvest`")
    a.add_argument("--project", default="")
    a.set_defaults(func=memory_adopt, needs_daemon=False)


def memory_help(args) -> int:
    print("usage: nodus debug memory {sources,harvest,adopt} ...")
    return 0


def _pin(root: str) -> None:
    """Point the board at the project you just looked at.

    `nodus up` starts the daemon inside the state directory, so the daemon's cwd
    cannot answer "which project is the operator reviewing". Recording the last
    project harvested is a good enough answer; `$NODUS_PROJECT` overrides it.
    """
    try:
        data = config.load_config()
        if data.get("project_dir") == root:
            return
        data["project_dir"] = root
        config.ensure_dirs()
        config.config_path().write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    except OSError:
        pass  # a read-only home must not stop a read-only command


def _root(args) -> str:
    root = memory.project_root(args.project or None)
    _pin(root)
    return root


def memory_sources(args) -> int:
    root = _root(args)
    rows = memory.sources(root)
    if not rows:
        print(f"{root}: 没有找到任何记忆文件（AGENTS.md / CLAUDE.md / ~/.codex 等）")
        return 0
    print(f"扫描 {root}（{len(rows)} 个文件）")
    for row in rows:
        print(f"  {row['scope']:<8} {row['harness']:<6} {row['path']}  ({row['lines']} 行)")
    return 0


def _label(row: dict, root: str) -> str:
    return "、".join(
        source.get("label") or memory.short_path(source["path"], root)
        for source in row["sources"]
    )


def memory_harvest(args) -> int:
    root = _root(args)
    rows = memory.harvest(root)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    pending = [row for row in rows if args.all or not row["adopted"]]
    merged = sum(len(row["sources"]) - 1 for row in rows)
    conflicts = sum(1 for row in rows if row["conflict"])
    adopted = sum(1 for row in rows if row["adopted"])
    print(
        f"{root}: {len(rows)} 条候选"
        f"（合并 {merged} 处重复，{conflicts} 条冲突，已采纳 {adopted} 条）"
    )
    shown = pending if not args.limit else pending[: args.limit]
    for row in shown:
        mark = "!" if row["conflict"] else " "
        done = "✓ " if row["adopted"] else ""
        print(f" {mark} {row['id']}  {done}{row['text']}")
        print(f"      ← {_label(row, root)}")
    if not pending:
        print("  （都采纳过了；--all 看全部）")
    elif len(shown) < len(pending):
        print(f"  …还有 {len(pending) - len(shown)} 条（--limit 0 全部打印，--json 机器可读）")
    return 0


def memory_adopt(args) -> int:
    root = _root(args)
    try:
        result = memory.adopt(args.ids, root)
    except ValueError as exc:
        raise ValueError(str(exc)) from None
    if result["added"]:
        print(f"采纳 {result['added']} 条 → {result['path']}")
    else:
        print(f"没有新内容：这些已经在 {result['path']} 里了")
    return 0


def memory_dream(args) -> int:
    from ... import dream
    if args.dry_run:
        result = dream.dry_run(args.limit)
    else:
        batch = dream.plan(limit=args.limit)
        result = dream.count(dream.extract(batch)) if batch else {"status": "idle"}
    print(json.dumps(result, ensure_ascii=False, indent=None if args.json else 2))
    return 0

def memory_reindex(args) -> int:
    root = _root(args); path = memory._project(root) / "AGENTS.md"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text, _ = memory.ensure_marker(text)
    from ... import db
    with db.connect() as c:
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (f"l3_hash:{path.resolve()}", memory.marker_hash(text)))
    path.write_text(text, encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(path)})); return 0

def memory_rollback(args) -> int:
    root = _root(args)
    print(json.dumps(memory.rollback(memory._project(root) / "AGENTS.md", args.to), ensure_ascii=False))
    return 0
