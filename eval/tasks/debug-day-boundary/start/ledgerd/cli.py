"""Command line: print a day or a month report for a ledger export.

    python -m ledgerd.cli 2024-03-01
    python -m ledgerd.cli --month 2024-03
    python -m ledgerd.cli --store data/march.csv 2024-03-05

Exit codes: 0 on success, 2 on a bad argument or an unreadable export, with
one ``error: ...`` line on stderr. The output format is consumed by the
month-end script and must not change.
"""

from __future__ import annotations

import sys

from .parsing import ParseError, parse_day, parse_month
from .report import day_report, month_report, render_day, render_month
from .store import Store, StoreError

USAGE = "usage: python -m ledgerd.cli [--store PATH] [--month] <day|YYYY-MM>"
DEFAULT_STORE = "data/march.csv"


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str]) -> int:
    store_path = DEFAULT_STORE
    want_month = False
    positional: list[str] = []

    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("-h", "--help"):
            print(USAGE)
            return 0
        if arg in ("-s", "--store"):
            if index + 1 >= len(argv):
                return _fail("--store needs a path")
            store_path = argv[index + 1]
            index += 2
            continue
        if arg in ("-m", "--month"):
            want_month = True
            index += 1
            continue
        if arg.startswith("-"):
            return _fail(f"unknown option {arg!r}")
        positional.append(arg)
        index += 1

    if len(positional) != 1:
        print(USAGE, file=sys.stderr)
        return 2

    try:
        store = Store.from_csv(store_path)
        if want_month:
            year, month = parse_month(positional[0])
            print(render_month(month_report(store, year, month)))
        else:
            print(render_day(day_report(store, parse_day(positional[0]))))
    except (ParseError, StoreError) as exc:
        return _fail(str(exc))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    raise SystemExit(main(sys.argv[1:]))
