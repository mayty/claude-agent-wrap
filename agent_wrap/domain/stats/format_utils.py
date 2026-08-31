# This file has been edited with the assistance of an AI tool.
"""Date/time helpers for the usage-stats domain."""

from agent_wrap.domain.stats.constants import UNKNOWN_TIME_KEY


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
    if day_key == UNKNOWN_TIME_KEY:
        return from_iso is None and until_iso is None
    if from_iso is not None and day_key < from_iso:
        return False
    return not (until_iso is not None and day_key > until_iso)
