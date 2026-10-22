"""Hidden checks for unit-cli: subprocess behaviour plus the two entry points."""

import subprocess
import sys
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parent.parent


def run_cli(*args):
    return subprocess.run(
        [sys.executable, str(WORK / "units.py"), *args],
        cwd=str(WORK),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )


def test_metres_to_kilometres():
    result = run_cli("1000", "m", "km")
    assert result.returncode == 0
    assert result.stdout.strip() == "1.000 km"


def test_miles_to_metres():
    result = run_cli("1", "mi", "m")
    assert result.returncode == 0
    assert result.stdout.strip() == "1609.344 m"


def test_centimetres_to_metres():
    result = run_cli("100", "cm", "m")
    assert result.returncode == 0
    assert result.stdout.strip() == "1.000 m"


def test_rounding_to_three_places():
    result = run_cli("2", "km", "mi")
    assert result.returncode == 0
    assert result.stdout.strip() == "1.243 mi"


def test_same_unit_round_trip():
    result = run_cli("42", "km", "km")
    assert result.returncode == 0
    assert result.stdout.strip() == "42.000 km"


@pytest.mark.parametrize(
    "args",
    [
        ("x", "m", "km"),
        ("1", "m"),
        ("1", "m", "km", "extra"),
        ("1", "m", "ft"),
        ("1", "parsec", "m"),
    ],
)
def test_bad_input_exits_2_with_an_error_line(args):
    result = run_cli(*args)
    assert result.returncode == 2
    assert result.stderr.strip().startswith("error:")
    assert result.stdout.strip() == ""


def test_convert_is_importable():
    sys.path.insert(0, str(WORK))
    try:
        import units
    finally:
        sys.path.pop(0)
    assert units.convert(1, "km", "m") == pytest.approx(1000.0)
    assert units.convert(1, "mi", "km") == pytest.approx(1.609344)


def test_main_returns_the_exit_code(capsys):
    sys.path.insert(0, str(WORK))
    try:
        import units
    finally:
        sys.path.pop(0)
    assert units.main(["500", "m", "km"]) == 0
    assert capsys.readouterr().out.strip() == "0.500 km"
    assert units.main(["nope", "m", "km"]) == 2
