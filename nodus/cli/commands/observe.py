"""nodus debug observe: read a harness's own usage records, never its mind.

    dsh         session.v4.jsonl.zstd files, priced into observation rows
    cc-switch   a read-only coverage probe over CC Switch's usage tables
    magpie      a read-only coverage probe over the local router's request log

Dry-run is the default so an operator can see what would change before the DB
changes. cc-switch and magpie have no import path: their numbers are USD
list-price estimates / token counts without money, not what the account was
charged, so they are never written into the ledger.
"""

from __future__ import annotations

from ... import observe, observe_ccswitch, observe_magpie

HARNESSES = ("dsh", "cc-switch", "magpie")


def add(sub) -> None:
    p = sub.add_parser(
        "observe",
        help="read harness-native usage records (dry-run by default; --import stores dsh)",
    )
    p.add_argument(
        "harness",
        nargs="?",
        default="dsh",
        choices=list(HARNESSES),
        help="dsh: session logs -> observation rows; cc-switch / magpie: read-only probes",
    )
    p.add_argument(
        "--dry-run",
        dest="do_import",
        action="store_false",
        help="scan and price, write nothing (default)",
    )
    p.add_argument(
        "--import", dest="do_import", action="store_true", help="write new observation rows (dsh only)"
    )
    p.add_argument(
        "--force", action="store_true", help="rescan files whose cursor says unchanged"
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable summary"
    )
    # Two actions share the do_import dest; without this the store_true action
    # would leave it True and a bare 'observe dsh' would import. Dry-run is the
    # documented default, so pin it once.
    p.set_defaults(func=observe_cmd, needs_daemon=False, do_import=False)


def _report(result: dict) -> None:
    print(f"harness: dsh  root: {result['root']}")
    print(
        f"files: {result['files']}  scanned: {result['scanned']}"
        f"  skipped: {result['skipped']}  messages: {result['messages']}"
        f"  new rows: {result['inserted']}  unpriced: {result['unpriced']}"
    )
    tokens = result["tokens"]
    print(
        f"tokens: input {tokens['input']:,}  output {tokens['output']:,}"
        f"  cache_read {tokens['cache_read']:,}  cache_write {tokens['cache_write']:,}"
    )
    cost = " + ".join(
        f"{currency} {amount:.4f}" for currency, amount in sorted(result["cost"].items())
    )
    print(f"cost: {cost or '-'}")
    for day, bucket in sorted(result["days"].items())[-8:]:
        money = " + ".join(
            f"{currency} {amount:.4f}" for currency, amount in sorted(bucket["cost"].items())
        )
        print(
            f"  {day}  {bucket['messages']:>5} msgs"
            f"  {bucket['tokens']:>13,} tokens  {money or '-'}"
        )


def _fmt_tokens(tokens: dict) -> str:
    total = sum(tokens.values())
    if total >= 1_000_000_000:
        return f"{total / 1_000_000_000:.1f}B tok"
    if total >= 1_000_000:
        return f"{total / 1_000_000:.1f}M tok"
    return f"{total:,} tok"


def _report_ccswitch(result: dict) -> None:
    print(f"harness: cc-switch  db: {result['db']}")
    if not result.get("available"):
        print(result.get("error") or "no usage tables found")
        for note in result.get("notes", []):
            print(f"  note: {note}")
        return
    for key, label in (
        ("rollups", "rollups（session 日志导入的日汇总，USD 官价估算）"),
        ("proxy", "proxy（本地代理逐请求日志，USD 官价估算）"),
    ):
        block = result.get(key)
        if not block:
            print(f"{label}: -")
            continue
        window = f"{block['start']} → {block['end']}" if block["start"] else "?"
        print(
            f"{label}: {block['rows']} rows, {block['requests']:,} req, "
            f"{_fmt_tokens(block['tokens'])}, {window}, "
            f"USD {block['estimated_usd']:,.2f}（估算）"
        )
        if block.get("zero_cost_requests"):
            print(f"  zero-cost requests（缺价/未计价）: {block['zero_cost_requests']:,}")
        for app, bucket in sorted(block["apps"].items(), key=lambda kv: -kv[1]["requests"]):
            print(
                f"  {app:<10} {bucket['requests']:>8,} req  "
                f"{_fmt_tokens(bucket['tokens']):>10}  "
                f"USD {bucket['estimated_usd']:>10,.2f}"
            )
    for note in result.get("notes", []):
        print(f"note: {note}")


def _report_magpie(result: dict) -> None:
    print(f"harness: magpie  log: {result['path']}")
    if not result.get("available"):
        print(result.get("error") or "no requests in the log")
        for note in result.get("notes", []):
            print(f"  note: {note}")
        return
    print(
        f"requests: {result['requests']:,}  failed: {result['failed']:,}  "
        f"window: {result['start']} → {result['end']}  "
        f"avg: {result['ms'] / max(result['requests'], 1):.0f} ms"
    )
    print(
        "tokens: input %s  output %s  cache_read %s  cache_write %s"
        % (
            f"{result['tokens']['input']:,}",
            f"{result['tokens']['output']:,}",
            f"{result['tokens']['cache_read']:,}",
            f"{result['tokens']['cache_write']:,}",
        )
    )
    print("by agent:")
    for agent, b in sorted(result["by_agent"].items(), key=lambda kv: -kv[1]["requests"])[:8]:
        print(
            f"  {agent:<16} {b['requests']:>6,} req  {_fmt_tokens(b['tokens']):>10}  "
            f"failed {b['failed']}"
        )
    print("by provider:")
    for provider, b in sorted(
        result["by_provider"].items(), key=lambda kv: -kv[1]["requests"]
    )[:6]:
        print(
            f"  {provider:<16} {b['requests']:>6,} req  {_fmt_tokens(b['tokens']):>10}  "
            f"failed {b['failed']}"
        )
    for note in result.get("notes", []):
        print(f"note: {note}")


def observe_cmd(args) -> int:
    if args.harness == "magpie":
        if args.do_import:
            print("error: magpie 是只读探针；日志只有 token、没有金额，不写进 observation 表")
            return 2
        result = observe_magpie.scan()
        if args.as_json:
            import json

            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0
        _report_magpie(result)
        return 0
    if args.harness == "cc-switch":
        if args.do_import:
            print("error: cc-switch 是只读探针；金额是美元估算，不写进 observation 表")
            return 2
        result = observe_ccswitch.scan()
        if args.as_json:
            import json

            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0
        _report_ccswitch(result)
        return 0
    result = observe.scan(dry_run=not args.do_import, force=args.force)
    if args.as_json:
        import json

        print(json.dumps(result, ensure_ascii=False))
        return 0
    _report(result)
    if not args.do_import:
        print("(dry-run: nothing written; add --import to store new rows)")
    return 0
