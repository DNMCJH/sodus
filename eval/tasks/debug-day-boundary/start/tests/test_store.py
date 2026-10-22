"""The store's day/month windows are half-open, matching end_of_day."""

from datetime import date
from pathlib import Path

import pytest

from ledgerd.store import Store, StoreError

DATA = Path(__file__).resolve().parent.parent / "data" / "march.csv"


def test_csv_loads_every_row():
    store = Store.from_csv(DATA)
    assert len(store) == 11
    assert store.currencies == ("CNY", "EUR", "USD")


def test_day_window_includes_midnight_and_last_microsecond():
    store = Store.from_csv(DATA)
    ids = [record.id for record in store.on_day(date(2024, 3, 1))]
    assert ids == ["tx-001", "tx-002", "tx-003"]


def test_midnight_batch_belongs_to_the_next_day():
    store = Store.from_csv(DATA)
    ids = [record.id for record in store.on_day(date(2024, 3, 2))]
    assert ids == ["tx-004", "tx-005", "tx-006"]


def test_month_window_stops_before_the_next_month():
    store = Store.from_csv(DATA)
    ids = [record.id for record in store.on_month(2024, 3)]
    assert len(ids) == 10
    assert "tx-011" not in ids


def test_a_bad_currency_names_its_line(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        "id,ts,currency,amount\n"
        "a,2024-03-01 00:00:00,CNY,1.00\n"
        "b,2024-03-01 01:00:00,C1,2.00\n",
        encoding="utf-8",
    )
    with pytest.raises(StoreError, match=r"bad\.csv:3"):
        Store.from_csv(bad)
