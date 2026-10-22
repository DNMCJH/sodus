"""Regional sales report from a CSV of order lines."""

from __future__ import annotations

import json
import sys

from sales.report import build_report


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("error: usage: python report_cli.py <file.csv>", file=sys.stderr)
        return 2
    try:
        report = build_report(argv[0])
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
