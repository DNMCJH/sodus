"""Parsing and boundary helpers for the day ledger.

Everything in the service goes through this module: CSV timestamps are parsed
here, day and month boundaries are computed here, and money is rounded here.
The boundary functions are the ones to read carefully:

- ``day_start(day)`` is the first instant of ``day`` (inclusive).
- ``end_of_day(day)`` is the first instant of the *next* day, so it is
  **exclusive**: there is no instant that is both in ``day`` and equal to
  ``end_of_day(day)``.
- ``in_range(ts, start, end)`` is inclusive on both ends. It is a comparison
  helper, not a day filter; callers that pass an exclusive boundary have to
  adjust it before calling (see ``store.Store.on_day``).
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
_AMOUNT_NOISE = re.compile(r"[\s,$¥€£]")
_DAY_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d.%m.%Y")
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
)


class ParseError(ValueError):
    """A value in the input could not be turned into a date, time or amount."""


def parse_day(text: str) -> date:
    """Read a calendar day from the handful of formats the exports use."""
    raw = (text or "").strip()
    if not raw:
        raise ParseError("empty day")
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ParseError(f"not a day: {raw!r}")


def parse_ts(text: str) -> datetime:
    """Read a naive local timestamp. Microseconds are kept when present."""
    raw = (text or "").strip()
    if not raw:
        raise ParseError("empty timestamp")
    for fmt in _TS_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ParseError(f"not a timestamp: {raw!r}")


def day_start(day: date) -> datetime:
    """The first instant of ``day`` (inclusive)."""
    return datetime.combine(day, time.min)


def end_of_day(day: date) -> datetime:
    """The first instant of the next day: an **exclusive** upper bound."""
    return day_start(day) + timedelta(days=1)


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last calendar day of a month, validated."""
    if not 1 <= int(year) <= 9999:
        raise ParseError(f"year out of range: {year}")
    if not 1 <= int(month) <= 12:
        raise ParseError(f"month out of range: {month}")
    first = date(int(year), int(month), 1)
    if first.month == 12:
        following = date(first.year + 1, 1, 1)
    else:
        following = date(first.year, first.month + 1, 1)
    return first, following - timedelta(days=1)


def parse_month(text: str) -> tuple[int, int]:
    """Read ``YYYY-MM`` (or ``YYYY/MM``) into ``(year, month)``."""
    raw = (text or "").strip()
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})", raw)
    if not match:
        raise ParseError(f"not a month: {raw!r}")
    year, month = int(match.group(1)), int(match.group(2))
    month_bounds(year, month)  # validation only
    return year, month


def in_range(ts: datetime, start: datetime, end: datetime) -> bool:
    """``start <= ts <= end``. Both ends are inclusive by contract."""
    return start <= ts <= end


def parse_amount(text: str) -> Decimal:
    """Read money as an exact Decimal, tolerating currency signs and commas."""
    raw = _AMOUNT_NOISE.sub("", str(text or ""))
    if not raw:
        raise ParseError("empty amount")
    try:
        return Decimal(raw)
    except InvalidOperation:
        raise ParseError(f"not an amount: {text!r}") from None


def money(value: Decimal | int | float | str) -> Decimal:
    """Round to whole cents, half away from zero. No floats in the result."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation:
        raise ParseError(f"not an amount: {value!r}") from None
    return amount.quantize(CENT, rounding=ROUND_HALF_UP)


def render_money(value: Decimal) -> str:
    """Two fixed decimals, no thousands separator: ``120.50``."""
    return f"{money(value):.2f}"
