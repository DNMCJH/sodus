"""Quote a cart: subtotal, regional tax, total."""

from __future__ import annotations

from .models import LineItem
from .tax import tax_for


def subtotal(items: list[LineItem]) -> float:
    """Sum of qty * unit_price, rounded to cents."""
    return round(sum(item.qty * item.unit_price for item in items), 2)


def quote(
    items: list[LineItem], region: str = "none", *, customer_tier: str = "standard"
) -> dict:
    """Return {"subtotal", "tax", "total"} for the cart, all rounded to cents."""
    base = subtotal(items)
    tax = tax_for(base, region)
    return {"subtotal": base, "tax": tax, "total": round(base + tax, 2)}
