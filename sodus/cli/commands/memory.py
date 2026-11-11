"""`sodus debug memory`: what the harnesses remembered, and what to adopt.

Memory is not a Sodus table. It is the plain text each harness already keeps —
`CLAUDE.md`, `AGENTS.md` — and the one thing missing is a summary a human can
review. These commands read those files directly: no daemon, no database, and
`sodus debug memory` keeps working when `sodus up` is down.
"""

from __future__ import annotations

import json
import time

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

    ms = sub.add_parser("memory-stats", help="memory rot and exposure statistics")
    ms.add_argument("--json", action="store_true")
    ms.set_defaults(func=memory_stats, needs_daemon=False)


def memory_help(args) -> int:
    print("usage: sodus debug memory {sources,harvest,adopt} ...")
    return 0


def _pin(root: str) -> None:
    """Point the board at the project you just looked at.

    `sodus up` starts the daemon inside the state directory, so the daemon's cwd
    cannot answer "which project is the operator reviewing". Recording the last
    project harvested is a good enough answer; `$SODUS_PROJECT` overrides it.
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


def memory_stats(args) -> int:
    """Report rebuildable memory health (rot) without changing the database."""
    from ... import db
    now = time.time()
    workspace = config.workspace_id()
    with db.connect() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM memory_candidate WHERE workspace=?", (workspace,))]
        exposures = c.execute("SELECT COUNT(*) FROM memory_exposure e JOIN memory_candidate c ON c.id=e.candidate_id WHERE c.workspace=?", (workspace,)).fetchone()[0]
        hits = sum(int(r.get("hits") or 0) for r in rows)
    active = [r for r in rows if r.get("status") == "active"]
    hashes = [r.get("hash") for r in rows if r.get("hash")]
    duplicate_hashes = len(hashes) - len(set(hashes))
    by_key = {}
    for r in active:
        if r.get("key"):
            by_key.setdefault(r["key"], set()).add(r.get("text"))
    contradictions = sum(len(texts) > 1 for texts in by_key.values())
    expired = sum(bool(r.get("expires_at") and float(r["expires_at"]) < now) or bool(r.get("last_seen") and float(r["last_seen"]) < now - 90 * 86400) for r in rows)
    weeks = {}
    for r in rows:
        stamp = float(r.get("created_at") or 0)
        key = time.strftime("%Y-W%W", time.localtime(stamp)) if stamp else "unknown"
        weeks[key] = weeks.get(key, 0) + 1
    lineage = {}
    for r in rows:
        try:
            source = json.loads(r.get("sources_json") or "[]")
        except (TypeError, ValueError):
            source = []
        roots = {s.get("lineage_root") for s in source if isinstance(s, dict) and s.get("lineage_root")}
        for root in roots:
            lineage[root] = lineage.get(root, 0) + 1
    result = {"workspace": workspace, "entries": len(rows), "duplicate_hashes": duplicate_hashes,
              "near_duplicates": 0, "contradictions": contradictions, "expired": expired,
              "expired_ratio": expired / len(rows) if rows else 0.0, "growth_by_week": weeks,
              "lineage_root_distribution": lineage, "exposures": exposures, "hits": hits,
              "exposure_hit_rate": hits / exposures if exposures else 0.0}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"memory entries={len(rows)} rot duplicate={duplicate_hashes} contradictions={contradictions} expired={expired}")
        print(f"exposures={exposures} hits={hits} hit-rate={result['exposure_hit_rate']:.1%}")
    return 0
