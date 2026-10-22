"""Regional sales tax."""

from __future__ import annotations

RATES = {"cn": 0.13, "us": 0.08, "jp": 0.10, "none": 0.0}


def tax_for(amount: float, region: str) -> float:
    """Tax on `amount` at the rate for `region`, rounded to cents."""
    try:
        rate = RATES[region]
    except KeyError:
        raise ValueError(f"unknown region: {region}") from None
    return round(amount * rate, 2)
