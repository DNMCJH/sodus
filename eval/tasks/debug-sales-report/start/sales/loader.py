"""Read the order-line CSV exactly as it is on disk."""

from __future__ import annotations

import csv
from pathlib import Path


def load_rows(path: str | Path) -> list[dict]:
    """Every data row as a string-keyed dict, in file order."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
