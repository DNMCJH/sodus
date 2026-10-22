"""Turn a raw CSV row into the record the aggregate layer consumes."""

from __future__ import annotations

from .normalize import parse_amount, parse_int


def to_record(row: dict) -> dict:
    """One typed record: lower-case region, upper-case SKU, integer units."""
    units = parse_int(row["units"])
    price = parse_amount(row["unit_price"])
    return {
        "region": str(row["region"]).strip().lower(),
        "sku": str(row["sku"]).strip().upper(),
        "units": units,
        "unit_price": price,
        "revenue": round(units * price, 2),
    }
