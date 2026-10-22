"""Contract tests for parsing: the boundary helpers mean exactly what they say."""

from datetime import date, datetime
from decimal import Decimal

import pytest

from ledgerd.parsing import (
    ParseError,
    day_start,
    end_of_day,
    in_range,
    money,
    month_bounds,
    parse_amount,
    parse_day,
    parse_month,
    parse_ts,
)


def test_parse_day_accepts_the_export_formats():
    assert parse_day("2024-03-01") == date(2024, 3, 1)
    assert parse_day("2024/03/01") == date(2024, 3, 1)
    assert parse_day("01.03.2024") == date(2024, 3, 1)


def test_parse_ts_keeps_microseconds():
    assert parse_ts("2024-03-01 23:59:59.999999") == datetime(2024, 3, 1, 23, 59, 59, 999999)
    assert parse_ts("2024-03-01T00:00:00") == datetime(2024, 3, 1)


def test_end_of_day_is_the_next_midnight_and_is_exclusive():
    stop = end_of_day(date(2024, 3, 1))
    assert stop == datetime(2024, 3, 2, 0, 0, 0)
    assert stop > datetime(2024, 3, 1, 23, 59, 59, 999999)


def test_in_range_includes_both_ends():
    start = day_start(date(2024, 3, 1))
    stop = datetime(2024, 3, 2, 0, 0, 0)
    assert in_range(start, start, stop) is True
    assert in_range(stop, start, stop) is True


def test_month_bounds_and_parse_month():
    assert month_bounds(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
    assert parse_month("2024-03") == (2024, 3)
    with pytest.raises(ParseError):
        month_bounds(2024, 13)


def test_amount_parsing_and_cent_rounding():
    assert parse_amount("$1,234.56") == Decimal("1234.56")
    assert money("2.675") == Decimal("2.68")
    with pytest.raises(ParseError):
        parse_amount("twelve")
