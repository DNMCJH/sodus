"""Report shape, console text and CLI exit codes."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ledgerd import cli
from ledgerd.ledger import daily_totals
from ledgerd.report import day_report, month_report, render_day, render_month
from ledgerd.store import Record, Store

DATA = Path(__file__).resolve().parent.parent / "data" / "march.csv"


def test_day_report_shape():
    report = day_report(Store.from_csv(DATA), date(2024, 3, 5))
    assert report["day"] == "2024-03-05"
    assert report["row_count"] == 2
    assert report["totals"] == {"CNY": Decimal("20.50")}
    assert [row["id"] for row in report["rows"]] == ["tx-007", "tx-008"]
    assert report["rows"][0]["ts"] == "2024-03-05 08:00:00.000000"


def test_render_day_shows_the_rows_and_the_total():
    text = render_day(day_report(Store.from_csv(DATA), date(2024, 3, 5)))
    assert text.splitlines()[0] == "2024-03-05  day report, 2 rows"
    assert text.splitlines()[-1] == "TOTAL CNY 20.50"


def test_month_report_uses_the_month_window():
    rows = [
        Record("a", datetime(2024, 2, 10, 8, 0), "CNY", Decimal("1.00"), ""),
        Record("b", datetime(2024, 2, 29, 23, 0), "CNY", Decimal("2.00"), ""),
    ]
    report = month_report(Store(rows), 2024, 2)
    assert report["month"] == "2024-02"
    assert report["row_count"] == 2
    assert report["totals"] == {"CNY": Decimal("3.00")}
    assert render_month(report).splitlines()[-1] == "TOTAL CNY 3.00"


def test_cli_prints_a_day_and_returns_zero(capsys):
    assert cli.main(["--store", str(DATA), "2024-03-05"]) == 0
    out = capsys.readouterr().out
    assert out.rstrip().endswith("TOTAL CNY 20.50")


def test_cli_rejects_a_bad_day(capsys):
    assert cli.main(["2024-13-40"]) == 2
    assert capsys.readouterr().err.startswith("error: ")
