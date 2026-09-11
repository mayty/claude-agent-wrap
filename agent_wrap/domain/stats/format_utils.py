# This file has been edited with the assistance of an AI tool.
"""Date/time helpers for the usage-stats domain."""

from datetime import UTC, datetime, timedelta

from agent_wrap.constants import DAY_START_HOURS
from agent_wrap.domain.stats.constants import (
    HOURS_PER_DAY,
    SECONDS_PER_HOUR,
    UNKNOWN_TIME_KEY,
)


def hour_bounds(from_iso: str | None, until_iso: str | None) -> tuple[int | None, int | None]:
    """
    Convert an inclusive stats-day window into the inclusive hour buckets it spans.

    An hour bucket is a count of whole hours since the epoch — the grain the usage
    index is keyed by. A stats day *D* is the UTC span
    ``[D + DAY_START_HOURS, D + 1 day + DAY_START_HOURS)``, and ``DAY_START_HOURS`` is a
    whole number of hours, so a day boundary always falls on an hour boundary and this
    conversion is exact rather than a widening.

    ``None`` on either side stays ``None`` — an open side imposes no bound.

    The caller still applies :func:`day_in_range` per cell afterwards, which is not
    redundant. That is where the ``"?"`` bucket's rule lives (a request with no
    timestamp belongs to no day and appears only in the all-time view), and stating the
    day rule once, in one place, is what keeps a bound in hours from becoming a second
    definition of what a day is.
    """
    return (
        None if from_iso is None else _day_start_hour(from_iso),
        None if until_iso is None else _day_start_hour(until_iso) + HOURS_PER_DAY - 1,
    )


def _day_start_hour(day_iso: str) -> int:
    """Return the hour bucket a stats day begins in."""
    midnight = datetime.fromisoformat(day_iso).replace(tzinfo=UTC)
    start = midnight + timedelta(hours=DAY_START_HOURS)
    return int(start.timestamp()) // SECONDS_PER_HOUR


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
