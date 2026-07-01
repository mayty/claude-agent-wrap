# This file has been created with the assistance of an AI tool.
"""Cost-formatting helpers for the pricing domain."""

from __future__ import annotations


def fmt_cost(c: float | None) -> str:
    """Format a cost value as ``$X.XX``, or ``?`` when the cost is unknown (None)."""
    if c is None:
        return "?"
    return f"${c:.2f}"


def fmt_cost_with_unknown(c: float | None, *, unknown: bool) -> str:
    """Format cost with an unknown flag, collapsing ``$0.00+?`` to just ``?``."""
    if c is None or (c == 0.0 and unknown):
        return "?"
    if unknown:
        return f"${c:.2f}+?"
    return f"${c:.2f}"
