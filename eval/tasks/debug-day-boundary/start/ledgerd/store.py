"""Loading and indexing the transaction records the ledger reports on.

The store is deliberately dumb: it parses a CSV once, sorts the records by
timestamp, and answers "which records belong to this day/month" using the
boundary helpers from ``parsing``. It does not sum anything; money lives in
``ledger``.

Day/month membership here uses a half-open interval ``[start, end)`` because
``parsing.end_of_day`` returns the next midnight, not the last microsecond of
the day. Getting that distinction wrong is the classic off-by-one in this
service: a batch that lands exactly at midnight belongs to the new day.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from .parsing import (
    ParseError,
    day_start,
    end_of_day,
    money,
    month_bounds,
    parse_amount,
    parse_ts,
)

REQUIRED_COLUMNS = ("id", "ts", "currency", "amount")


class StoreError(ValueError):
    """The input file does not look like a ledger export."""


@dataclass(frozen=True)
class Record:
    """One transaction row, with the amount already rounded to cents."""

    id: str
    ts: datetime
    currency: str
    amount: Decimal
    memo: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
            "currency": self.currency,
            "amount": self.amount,
            "memo": self.memo,
        }


def _clean_header(name: str | None) -> str:
    return (name or "").strip().lower().lstrip("\ufeff")


class Store:
    """An immutable, timestamp-sorted collection of records."""

    def __init__(self, records=()) -> None:
        rows = list(records)
        for row in rows:
            if not isinstance(row, Record):
                raise TypeError(f"not a Record: {row!r}")
        self._records = tuple(sorted(rows, key=lambda r: (r.ts, r.id)))
        self._currencies = tuple(sorted({r.currency for r in self._records}))

    # ---- construction -------------------------------------------------

    @classmethod
    def from_csv(cls, path: str | Path) -> "Store":
        """Read a ledger export. A bad row names its line in the error."""
        source = Path(path)
        try:
            handle = source.open("r", encoding="utf-8-sig", newline="")
        except OSError as exc:
            raise StoreError(f"cannot read {source}: {exc}") from None
        with handle:
            reader = csv.DictReader(handle)
            header = [_clean_header(name) for name in (reader.fieldnames or [])]
            missing = [name for name in REQUIRED_COLUMNS if name not in header]
            if missing:
                raise StoreError(
                    f"{source}: missing column(s) {', '.join(missing)}"
                )
            reader.fieldnames = header
            records = []
            for line_no, raw in enumerate(reader, start=2):
                if not any((value or "").strip() for value in raw.values()):
                    continue  # blank line between exports
                try:
                    records.append(cls._row(raw))
                except (ParseError, KeyError, TypeError) as exc:
                    raise StoreError(f"{source}:{line_no}: {exc}") from None
        return cls(records)

    @staticmethod
    def _row(raw: dict) -> Record:
        rid = (raw.get("id") or "").strip()
        if not rid:
            raise ParseError("row has no id")
        currency = (raw.get("currency") or "").strip().upper()
        if len(currency) != 3 or not currency.isalpha():
            raise ParseError(f"bad currency: {currency!r}")
        ts = parse_ts(raw.get("ts") or "")
        amount = money(parse_amount(raw.get("amount") or ""))
        memo = (raw.get("memo") or "").strip()
        return Record(id=rid, ts=ts, currency=currency, amount=amount, memo=memo)

    # ---- read side ----------------------------------------------------

    @property
    def records(self) -> tuple[Record, ...]:
        return self._records

    @property
    def currencies(self) -> tuple[str, ...]:
        return self._currencies

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    def on_day(self, day: date) -> tuple[Record, ...]:
        """Records in ``day``: ``start <= ts < end_of_day`` (exclusive end)."""
        start = day_start(day)
        stop = end_of_day(day)
        return tuple(r for r in self._records if start <= r.ts < stop)

    def on_month(self, year: int, month: int) -> tuple[Record, ...]:
        """Records in the month, again with an exclusive next-month start."""
        first, last = month_bounds(year, month)
        start = day_start(first)
        stop = end_of_day(last)
        return tuple(r for r in self._records if start <= r.ts < stop)

    def for_currency(self, currency: str) -> tuple[Record, ...]:
        wanted = (currency or "").strip().upper()
        return tuple(r for r in self._records if r.currency == wanted)

    def between(self, start: datetime, end: datetime) -> tuple[Record, ...]:
        """Half-open ``[start, end)``; used by export tooling, not by reports."""
        return tuple(r for r in self._records if start <= r.ts < end)
