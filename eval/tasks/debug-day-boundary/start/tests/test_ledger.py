"""Money contracts: intervals are inclusive, totals are per currency."""

from datetime import date, datetime
from decimal import Decimal

from ledgerd.ledger import (
    daily_totals,
    format_totals,
    monthly_totals,
    sum_between,
    totals_between,
)
from ledgerd.store import Record, Store


def record(rid, ts, currency, amount):
    return Record(id=rid, ts=ts, currency=currency, amount=Decimal(amount))


def test_sum_between_includes_both_ends():
    rows = [
        record("a", datetime(2024, 3, 1, 0, 0), "CNY", "1.00"),
        record("b", datetime(2024, 3, 1, 12, 0), "CNY", "2.00"),
        record("c", datetime(2024, 3, 2, 0, 0), "CNY", "4.00"),
    ]
    assert sum_between(rows, datetime(2024, 3, 1), datetime(2024, 3, 2)) == Decimal("7.00")
    assert sum_between(rows, datetime(2024, 3, 1, 12, 0), datetime(2024, 3, 1, 12, 0)) == Decimal("2.00")


def test_totals_between_groups_by_currency():
    rows = [
        record("a", datetime(2024, 3, 1, 0, 0), "CNY", "1.00"),
        record("b", datetime(2024, 3, 1, 1, 0), "USD", "3.50"),
        record("c", datetime(2024, 3, 1, 2, 0), "CNY", "2.25"),
    ]
    assert totals_between(rows, datetime(2024, 3, 1), datetime(2024, 3, 1, 23, 59)) == {
        "CNY": Decimal("3.25"),
        "USD": Decimal("3.50"),
    }


def test_daily_totals_on_a_day_without_a_midnight_neighbour():
    rows = [
        record("a", datetime(2024, 3, 5, 8, 0), "CNY", "12.00"),
        record("b", datetime(2024, 3, 5, 9, 0), "CNY", "8.50"),
    ]
    assert daily_totals(Store(rows), date(2024, 3, 5)) == {"CNY": Decimal("20.50")}


def test_monthly_totals_on_a_month_without_a_trailing_midnight_record():
    rows = [
        record("a", datetime(2024, 2, 10, 8, 0), "CNY", "1.00"),
        record("b", datetime(2024, 2, 29, 23, 0), "CNY", "2.00"),
    ]
    assert monthly_totals(Store(rows), 2024, 2) == {"CNY": Decimal("3.00")}


def test_format_totals_is_sorted_and_fixed_two_decimals():
    assert format_totals({"USD": Decimal("30"), "CNY": Decimal("120.5")}) == [
        "CNY 120.50",
        "USD 30.00",
    ]
