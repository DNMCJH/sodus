"""Primitive cell parsers: integers and money."""

from __future__ import annotations


def parse_int(text: str) -> int:
    """Parse an integer cell, allowing thousands separators."""
    cleaned = str(text).strip().replace(",", "")
    if not cleaned:
        raise ValueError("empty integer")
    return int(cleaned)


def parse_amount(text: str) -> float:
    """Parse a money cell such as "$1,234.50", rounded to cents."""
    cleaned = str(text).strip().replace("$", "").replace(",", "")
    if not cleaned:
        raise ValueError("empty amount")
    return int(float(cleaned) * 100) / 100
