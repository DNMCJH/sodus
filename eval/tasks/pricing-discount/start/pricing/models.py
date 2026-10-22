"""The shapes the pricing code passes around."""

from __future__ import annotations

from dataclasses import dataclass

TIER_STANDARD = "standard"
TIER_VIP = "vip"


@dataclass(frozen=True)
class LineItem:
    sku: str
    qty: int
    unit_price: float
