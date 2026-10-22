"""Per-region totals, in the shape the CLI prints."""

from __future__ import annotations


def summarize(records: list[dict]) -> dict:
    """Sum units and revenue per region, then add the grand total."""
    regions: dict[str, dict] = {}
    for record in records:
        bucket = regions.setdefault(record["region"], {"units": 0, "revenue": 0.0})
        bucket["units"] += record["units"]
        bucket["revenue"] = round(bucket["revenue"] + record["revenue"], 2)
    ordered = {name: regions[name] for name in sorted(regions)}
    total = round(sum(bucket["revenue"] for bucket in ordered.values()), 2)
    return {"regions": ordered, "total_revenue": total}
