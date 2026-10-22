"""Stock bookkeeping: quantities keyed by a normalized SKU."""

from __future__ import annotations


def _normalize(sku: str) -> str:
    parts = sku.strip().upper().replace("_", "-").replace(" ", "-").split("-")
    return "-".join(part for part in parts if part)


def add_sku(state: dict[str, int], sku: str, qty: int) -> dict[str, int]:
    """Add `qty` units for `sku`, merging keys that normalize to the same SKU."""
    key = _normalize(sku)
    state[key] = state.get(key, 0) + int(qty)
    return state
