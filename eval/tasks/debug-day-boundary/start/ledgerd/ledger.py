"""The money side of the service: summing records into per-currency totals.

One rule matters here and is easy to get wrong: ``sum_between`` and
``totals_between`` take an **inclusive** interval. ``parsing.end_of_day``
returns the next midnight, which is *exclusive*, so a caller that wants a
whole day must convert one convention into the other before calling. The
report layer is the only caller that has to care; the tests pin the inclusive
contract because the exports (and the tax fixtures) rely on it.

Records are exact Decimals; totals are rounded to cents only at the end.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from .parsing import day_start, end_of_day, in_range, money, month_bounds
from .store import Record, Store


def sum_between(
    records,
    start: datetime,
    end: datetime,
    currency: str | None = None,
) -> Decimal:
    """Sum amounts in ``[start, end]`` — both ends inclusive.

    ``currency=None`` sums every record regardless of currency, which is only
    meaningful for a single-currency fixture; reports always pass a currency.
    """
    wanted = (currency or "").strip().upper() or None
    total = Decimal("0")
    for record in records:
        if not isinstance(record, Record):
            raise TypeError(f"not a Record: {record!r}")
        if wanted is not None and record.currency != wanted:
            continue
        if in_range(record.ts, start, end):
            total += record.amount
    return money(total)


def totals_between(records, start: datetime, end: datetime) -> dict[str, Decimal]:
    """Per-currency totals for ``[start, end]``, both ends inclusive."""
    buckets: dict[str, Decimal] = {}
    for record in records:
        if not isinstance(record, Record):
            raise TypeError(f"not a Record: {record!r}")
        if not in_range(record.ts, start, end):
            continue
        buckets[record.currency] = buckets.get(record.currency, Decimal("0")) + record.amount
    return {currency: money(buckets[currency]) for currency in sorted(buckets)}


def totals_of(records) -> dict[str, Decimal]:
    """Per-currency totals for a record sequence, no time filtering."""
    return totals_between(records, datetime.min, datetime.max)


def daily_totals(store: Store, day: date) -> dict[str, Decimal]:
    """Totals for one calendar day.

    ``end_of_day`` already carries the day all the way to the following
    midnight, so passing it straight through covers the whole day.
    """
    return totals_between(store.records, day_start(day), end_of_day(day))


def monthly_totals(store: Store, year: int, month: int) -> dict[str, Decimal]:
    """Totals for a calendar month, same boundary treatment as a day."""
    first, last = month_bounds(year, month)
    return totals_between(store.records, day_start(first), end_of_day(last))


def daily_total(store: Store, day: date, currency: str = "CNY") -> Decimal:
    """One currency's total for a day; a thin convenience over daily_totals."""
    return daily_totals(store, day).get((currency or "").strip().upper(), money(0))


def format_totals(totals: dict[str, Decimal]) -> list[str]:
    """``["CNY 120.50", "USD 30.00"]``, currencies sorted."""
    return [f"{currency} {money(amount):.2f}" for currency, amount in sorted(totals.items())]


def widest_currency(totals: dict[str, Decimal]) -> str:
    """The currency with the largest total, or ``""`` when there is nothing."""
    if not totals:
        return ""
    return max(sorted(totals), key=lambda currency: totals[currency])
