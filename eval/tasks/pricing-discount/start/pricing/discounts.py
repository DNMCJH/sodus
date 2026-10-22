"""Discount rules.

The public surface is fixed; only the body of `discount_for` is missing.
"""

from __future__ import annotations

SAVE10_CODE = "SAVE10"
SAVE10_MIN = 100.0
SAVE10_OFF = 10.0
VIP_RATE = 0.10


def discount_for(
    subtotal: float, *, vip: bool = False, coupon: str | None = None
) -> float:
    """Return the discount for `subtotal` (before tax), rounded to 2 decimals.

    Rules:
    - VIP gets VIP_RATE off the subtotal.
    - Coupon SAVE10 gives SAVE10_OFF off, but only when subtotal >= SAVE10_MIN.
    - At most one rule applies: keep the larger discount. An unknown or empty
      coupon is ignored, never an error.
    - `subtotal` is a non-negative amount; a negative subtotal returns 0.0.
    - The result is never negative and never exceeds a non-negative `subtotal`.
    """
    raise NotImplementedError("discount_for")
