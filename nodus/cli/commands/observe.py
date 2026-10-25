"""nodus debug observe: read a harness session log into the observation table.

The harness keeps writing its own log; this command only reads it. Dry-run by
default so an operator can see what would be added before the DB changes.
"""

from __future__ import annotations

from ... import observe


def add(sub) -> None:
    p = sub.add_parser(
        "observe",
        help="read harness-native session logs (dry-run by default; --import stores)",
    )
    p.add_argument(
        "harness", nargs="?", default="dsh", choices=["dsh"], help="currently dsh"
    )
    p.add_argument(
        "--dry-run",
        dest="do_import",
        action="store_false",
        help="scan and price, write nothing (default)",
    )
    p.add_argument(
        "--import", dest="do_import", action="store_true", help="write new observation rows"
    )
    p.add_argument(
        "--force", action="store_true", help="rescan files whose cursor says unchanged"
    )
    p.add_argument(
        "--json", action="store_true", dest="as_json", help="machine-readable summary"
    )
    p.set_defaults(func=observe_cmd, needs_daemon=False)


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


def observe_cmd(args) -> int:
    result = observe.scan(dry_run=not args.do_import, force=args.force)
    if args.as_json:
        import json

        print(json.dumps(result, ensure_ascii=False))
        return 0
    _report(result)
    if not args.do_import:
        print("(dry-run: nothing written; add --import to store new rows)")
    return 0
