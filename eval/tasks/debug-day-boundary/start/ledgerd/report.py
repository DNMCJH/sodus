"""The report layer: one day or one month, ready for the CLI.

Rows come from ``Store.on_day`` / ``Store.on_month`` (half-open day bounds);
money comes from ``ledger.daily_totals`` / ``ledger.monthly_totals``. Those two
paths are supposed to describe the same set of records — the report's row count
is what the console prints above the total, and an accountant reads them
together.
"""

from __future__ import annotations

from datetime import date

from .ledger import daily_totals, format_totals, monthly_totals
from .parsing import money, month_bounds
from .store import Store


def day_report(store: Store, day: date) -> dict:
    """Rows and per-currency totals for one calendar day."""
    rows = store.on_day(day)
    return {
        "day": day.isoformat(),
        "row_count": len(rows),
        "rows": [record.as_dict() for record in rows],
        "totals": daily_totals(store, day),
    }


def month_report(store: Store, year: int, month: int) -> dict:
    """Rows and per-currency totals for a calendar month."""
    first, _last = month_bounds(year, month)
    rows = store.on_month(year, month)
    return {
        "month": f"{first.year:04d}-{first.month:02d}",
        "row_count": len(rows),
        "rows": [record.as_dict() for record in rows],
        "totals": monthly_totals(store, year, month),
    }


def render_day(report: dict) -> str:
    """The console text for ``day_report``: rows, then one TOTAL per currency."""
    lines = [
        f"{report['day']}  day report, {report['row_count']} rows",
    ]
    for row in report["rows"]:
        lines.append(
            f"  {row['ts']}  {row['currency']}  {money(row['amount']):>10.2f}  {row['memo']}"
        )
    totals = report["totals"]
    if not totals:
        lines.append("TOTAL 0.00")
    else:
        lines.extend(f"TOTAL {line}" for line in format_totals(totals))
    return "\n".join(lines)


def render_month(report: dict) -> str:
    """The console text for ``month_report``: a count and one TOTAL per currency."""
    lines = [f"{report['month']}  month report, {report['row_count']} rows"]
    totals = report["totals"]
    if not totals:
        lines.append("TOTAL 0.00")
    else:
        lines.extend(f"TOTAL {line}" for line in format_totals(totals))
    return "\n".join(lines)
