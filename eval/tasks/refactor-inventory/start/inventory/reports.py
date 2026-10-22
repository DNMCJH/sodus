"""Report helpers: quantity totals per normalized SKU."""

from __future__ import annotations


def _normalize(sku: str) -> str:
    count = 0
    out = []
    for chunk in sku.strip().upper().replace("_", "-").replace(" ", "-").split("-"):
        if chunk:
            out.append(chunk)
            count += len(chunk)
    return "-".join(out) if count or out else ""


def sku_summary(entries: list[dict]) -> dict[str, int]:
    """Sum quantities from `{"sku": ..., "qty": ...}` rows by normalized SKU."""
    totals: dict[str, int] = {}
    for entry in entries:
        key = _normalize(str(entry.get("sku", "")))
        totals[key] = totals.get(key, 0) + int(entry.get("qty", 0))
    return totals
