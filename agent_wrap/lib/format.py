# This file has been edited with the assistance of an AI tool.
"""Formatting helpers shared by the usage-stats subcommands."""

from __future__ import annotations

import sys
from datetime import datetime, timezone

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


def epoch_to_dt(x: float | None) -> datetime | None:
    """Convert a Unix epoch-seconds float to a UTC-aware datetime, or None."""
    if x is None:
        return None
    try:
        return datetime.fromtimestamp(x, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone().strftime("%Y-%m-%d")


def day_in_range(day_key: str, from_iso: str | None, until_iso: str | None) -> bool:
    """
    Report whether a ``YYYY-MM-DD`` day key falls within an inclusive range.

    ``from_iso``/``until_iso`` are inclusive ISO-date bounds (or None for open).
    Day keys and bounds are fixed-width zero-padded ISO dates, so lexicographic
    string comparison matches chronological order — no parsing needed.

    The synthetic ``"?"`` key (records with no timestamp) cannot be range-checked,
    so it is included only when the range is fully open (both bounds None), i.e.
    the all-time view.
    """
    if day_key == "?":
        return from_iso is None and until_iso is None
    if from_iso is not None and day_key < from_iso:
        return False
    return not (until_iso is not None and day_key > until_iso)
