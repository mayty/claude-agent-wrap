# This file has been created with the assistance of an AI tool.
"""Calendar-day bucketing with a configurable day-start offset from UTC midnight."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import date


def get_day(dt: datetime, day_start_hours: int) -> date:
    """
    Return the calendar day *dt* falls into, given a day-start offset from UTC midnight.

    *dt* must be UTC-aware (e.g. as returned by ``epoch_to_dt``). *day_start_hours*
    is how many hours past UTC midnight a day begins — negative values start a day
    before UTC midnight. Pure arithmetic; the caller is responsible for keeping
    *day_start_hours* in a sane range.
    """
    return (dt - timedelta(hours=day_start_hours)).date()


def local_utc_offset_hours() -> int:
    """Return the host's current local UTC offset, rounded to the nearest whole hour."""
    now_utc = datetime.now(tz=timezone.utc)
    offset = now_utc.astimezone().utcoffset()
    assert offset is not None
    return round(offset.total_seconds() / 3600)
