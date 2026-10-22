"""The one function the CLI calls."""

from __future__ import annotations

from pathlib import Path

from .aggregate import summarize
from .loader import load_rows
from .transform import to_record


def build_report(path: str | Path) -> dict:
    """Load `path`, type every row, and return the regional report body."""
    rows = load_rows(path)
    return summarize([to_record(row) for row in rows])
