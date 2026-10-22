"""Hidden checks for debug-day-boundary: symptom gone, contracts intact.

The report is the only thing that was wrong: a day's money used an exclusive
boundary as if it were inclusive. These checks pin both sides — the correct
totals and the inclusive/exclusive contracts the existing suite already
depends on — so "fix" by changing a shared helper is caught here even if the
agent rewrote tests/.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ledgerd.ledger import daily_totals, sum_between
from ledgerd.parsing import end_of_day
from ledgerd.report import day_report, month_report
from ledgerd.store import Record, Store

WORK = Path(__file__).resolve().parent.parent


def _store() -> Store:
    return Store.from_csv(WORK / "data" / "march.csv")


def test_march_first_rows_and_total_agree():
    report = day_report(_store(), date(2024, 3, 1))
    assert report["row_count"] == 3
    assert report["totals"] == {"CNY": Decimal("120.50")}
    assert sum(row["amount"] for row in report["rows"]) == report["totals"]["CNY"]


def test_midnight_batch_belongs_to_the_next_day():
    report = day_report(_store(), date(2024, 3, 2))
    assert report["row_count"] == 3
    assert report["totals"] == {"CNY": Decimal("292.25"), "USD": Decimal("30.00")}


def test_month_total_excludes_the_next_month():
    report = month_report(_store(), 2024, 3)
    assert report["row_count"] == 10
    assert report["totals"] == {
        "CNY": Decimal("438.25"),
        "EUR": Decimal("7.77"),
        "USD": Decimal("30.00"),
    }


def test_own_midnight_and_last_microsecond_count():
    rows = [
        Record("a", datetime(2024, 3, 1, 0, 0, 0), "CNY", Decimal("1.00"), ""),
        Record("b", datetime(2024, 3, 1, 23, 59, 59, 999999), "CNY", Decimal("2.00"), ""),
        Record("c", datetime(2024, 3, 2, 0, 0, 0), "CNY", Decimal("4.00"), ""),
    ]
    assert daily_totals(Store(rows), date(2024, 3, 1)) == {"CNY": Decimal("3.00")}
    assert daily_totals(Store(rows), date(2024, 3, 2)) == {"CNY": Decimal("4.00")}


def test_sum_between_is_still_inclusive_at_both_ends():
    rows = [
        Record("a", datetime(2024, 3, 1, 0, 0, 0), "CNY", Decimal("1.00"), ""),
        Record("b", datetime(2024, 3, 1, 12, 0, 0), "CNY", Decimal("2.00"), ""),
        Record("c", datetime(2024, 3, 2, 0, 0, 0), "CNY", Decimal("4.00"), ""),
    ]
    assert sum_between(rows, datetime(2024, 3, 1), datetime(2024, 3, 2)) == Decimal("7.00")


def test_end_of_day_is_still_the_next_midnight():
    assert end_of_day(date(2024, 3, 1)) == datetime(2024, 3, 2, 0, 0, 0)


def test_cli_prints_the_documented_totals():
    day = subprocess.run(
        [sys.executable, "-m", "ledgerd.cli", "2024-03-01"],
        cwd=WORK,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert day.returncode == 0
    assert "TOTAL CNY 120.50" in day.stdout
    assert "370.50" not in day.stdout

    month = subprocess.run(
        [sys.executable, "-m", "ledgerd.cli", "--month", "2024-03"],
        cwd=WORK,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert month.returncode == 0
    assert "TOTAL CNY 438.25" in month.stdout
    assert "TOTAL EUR 7.77" in month.stdout
    assert "TOTAL USD 30.00" in month.stdout


def test_the_existing_suite_is_green():
    for name in ("test_parsing.py", "test_store.py", "test_ledger.py", "test_report.py"):
        assert (WORK / "tests" / name).is_file(), f"tests/{name} was removed"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests"],
        cwd=WORK,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
