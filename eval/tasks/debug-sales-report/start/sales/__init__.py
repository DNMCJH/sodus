"""Sales report pipeline: load CSV -> typed records -> per-region totals."""

from .report import build_report

__all__ = ["build_report"]
