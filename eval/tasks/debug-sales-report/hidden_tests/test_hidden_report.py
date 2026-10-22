"""Hidden checks for the sales report: the numbers, not the shape."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from sales.normalize import parse_amount
from sales.report import build_report

EXPECTED = {
    "regions": {
        "north": {"revenue": 399.9, "units": 12},
        "south": {"revenue": 34.99, "units": 4},
    },
    "total_revenue": 434.89,
}


def test_parse_amount_rounds_to_cents():
    assert parse_amount("19.99") == 19.99
    assert parse_amount(" 0.29 ") == 0.29
    assert parse_amount("$1,234.56") == 1234.56
    assert parse_amount("5") == 5.0


def test_data_csv_report_is_exact():
    assert build_report(Path.cwd() / "data.csv") == EXPECTED


def test_another_csv_uses_the_same_rounding(tmp_path):
    csv_path = tmp_path / "other.csv"
    csv_path.write_text(
        "region,sku,units,unit_price\n"
        "north,A,1,19.99\n"
        "north,B,2,0.29\n"
        'south,C,1,"$1,234.56"\n',
        encoding="utf-8",
    )
    report = build_report(csv_path)
    assert report["regions"]["north"] == {"units": 3, "revenue": 20.57}
    assert report["regions"]["south"] == {"units": 1, "revenue": 1234.56}
    assert report["total_revenue"] == 1255.13


def test_cli_output_matches_the_documented_text():
    proc = subprocess.run(
        [sys.executable, "report_cli.py", "data.csv"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0
    assert proc.stdout == json.dumps(EXPECTED, indent=2, sort_keys=True) + "\n"


def test_cli_missing_file_is_an_error_exit():
    proc = subprocess.run(
        [sys.executable, "report_cli.py", "no-such-file.csv"],
        cwd=Path.cwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 1
    assert proc.stderr.startswith("error: ")
