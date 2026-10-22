"""Order lines: one ordered SKU with a price and a line total."""

from __future__ import annotations


def _normalize(sku: str) -> str:
    cleaned = sku.strip().upper().replace("_", "-").replace(" ", "-")
    return "-".join(part for part in cleaned.split("-") if part)


def order_line(sku: str, qty: int, unit_price: float) -> dict:
    """Return the line as stored: normalized SKU, integer quantity, 2dp total."""
    key = _normalize(sku)
    return {
        "sku": key,
        "qty": int(qty),
        "total": round(int(qty) * float(unit_price), 2),
    }
