# This file has been created with the assistance of an AI tool.
"""Calendar-day bucketing with a configurable day-start offset from UTC midnight."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from datetime import date


def epoch_to_dt(x: float | None) -> datetime | None:
    """Convert a Unix epoch-seconds float to a UTC-aware datetime, or None."""
    if x is None:
        return None
    try:
        return datetime.fromtimestamp(x, tz=UTC)
    except ValueError, OSError, OverflowError:
        return None


def get_day(dt: datetime, day_start_hours: int) -> date:
    """
    Return the calendar day *dt* falls into, given a day-start offset from UTC midnight.

    *dt* must be UTC-aware (e.g. as returned by :func:`epoch_to_dt` above). *day_start_hours*
    is how many hours past UTC midnight a day begins — negative values start a day
    before UTC midnight. Pure arithmetic; the caller is responsible for keeping
    *day_start_hours* in a sane range.
    """
    return (dt - timedelta(hours=day_start_hours)).date()


def local_utc_offset_hours() -> int:
    """Return the host's current local UTC offset, rounded to the nearest whole hour."""
    now_utc = datetime.now(tz=UTC)
    offset = now_utc.astimezone().utcoffset()
    assert offset is not None
    return round(offset.total_seconds() / 3600)


def utc_offset_hours_for_tz(tz_name: str) -> int:
    """Return the named IANA zone's current UTC offset, rounded to the nearest whole hour."""
    now_utc = datetime.now(tz=UTC)
    offset = now_utc.astimezone(ZoneInfo(tz_name)).utcoffset()
    assert offset is not None
    return round(offset.total_seconds() / 3600)
