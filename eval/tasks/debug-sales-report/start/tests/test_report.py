from pathlib import Path

from sales.report import build_report

DATA = Path(__file__).resolve().parent.parent / "data.csv"


def test_report_has_the_two_regions():
    assert set(build_report(DATA)["regions"]) == {"north", "south"}


def test_every_region_reports_units_and_money():
    report = build_report(DATA)
    for bucket in report["regions"].values():
        assert set(bucket) == {"units", "revenue"}
        assert isinstance(bucket["revenue"], float)
        assert bucket["units"] > 0
    assert isinstance(report["total_revenue"], float)
