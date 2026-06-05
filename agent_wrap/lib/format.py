# This file has been created with the assistance of an AI tool.
"""Formatting helpers shared by the usage-stats subcommands."""

from __future__ import annotations

import sys
from datetime import datetime

from agent_wrap.lib.console import Ansi

_THOUSAND = 1_000


def color(s: str, code: Ansi) -> str:
    return f"{code}{s}{Ansi.RESET}" if sys.stdout.isatty() else s


def fmt_count(n: int) -> str:
    # K is shown to one decimal; M and G to two.
    units = "K", "M", "G"

    if n < _THOUSAND:
        return str(n)

    value = float(n)
    for unit in units:
        value /= _THOUSAND
        if value < _THOUSAND:
            return f"{value:.1f}{unit}"

    return f"{value:.1f}{units[-1]}"


def fmt_cost(c: float | None) -> str:
    if c is None:
        return "?"
    return f"${c:.2f}"


def fmt_cost_with_unknown(c: float | None, *, unknown: bool) -> str:
    """Format cost, collapsing '$0.00+?' to just '?'."""
    if c is None or (c == 0.0 and unknown):
        return "?"
    if unknown:
        return f"${c:.2f}+?"
    return f"${c:.2f}"


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")
