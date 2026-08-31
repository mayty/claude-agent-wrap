# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/daytime.py."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfoNotFoundError

import pytest

from agent_wrap.lib.daytime import get_day, local_utc_offset_hours, utc_offset_hours_for_tz

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_get_day_zero_offset_matches_utc_date() -> None:
    dt = datetime(2026, 6, 15, 10, 30, 0, tzinfo=UTC)
    assert get_day(dt, 0) == date(2026, 6, 15)


def test_get_day_positive_offset_pushes_early_hours_to_previous_day() -> None:
    dt = datetime(2026, 6, 15, 1, 0, 0, tzinfo=UTC)
    assert get_day(dt, 2) == date(2026, 6, 14)


def test_get_day_negative_offset_pushes_late_hours_to_next_day() -> None:
    dt = datetime(2026, 6, 15, 23, 0, 0, tzinfo=UTC)
    assert get_day(dt, -2) == date(2026, 6, 16)


@pytest.mark.parametrize(
    ("hour", "day_start_hours", "expected"),
    [
        (0, 0, date(2026, 6, 15)),
        (23, 0, date(2026, 6, 15)),
        (0, 5, date(2026, 6, 14)),
        (4, 5, date(2026, 6, 14)),
        (5, 5, date(2026, 6, 15)),
    ],
)
def test_get_day_boundary_crossing(hour: int, day_start_hours: int, expected: date) -> None:
    dt = datetime(2026, 6, 15, hour, 0, 0, tzinfo=UTC)
    assert get_day(dt, day_start_hours) == expected


# The host's local timezone (what bare `.astimezone()` resolves to) isn't
# controllable from here, so `datetime.now` is mocked to return a stand-in
# whose `.astimezone()` result is fully scripted.


def _mock_now(mocker: MockerFixture, offset: timedelta) -> None:
    # datetime.datetime is immutable, so `now` can't be patched directly on it --
    # patch the module-level `datetime` name that daytime.py calls instead.
    fake_local = mocker.Mock(spec=datetime)
    fake_local.utcoffset.return_value = offset
    fake_now = mocker.Mock(spec=datetime)
    fake_now.astimezone.return_value = fake_local
    mock_datetime = mocker.patch("agent_wrap.lib.daytime.datetime")
    mock_datetime.now.return_value = fake_now


def test_local_utc_offset_hours_zero_offset(mocker: MockerFixture) -> None:
    _mock_now(mocker, timedelta(hours=0))
    assert local_utc_offset_hours() == 0


def test_local_utc_offset_hours_rounds_to_nearest_hour(mocker: MockerFixture) -> None:
    _mock_now(mocker, timedelta(hours=5, minutes=45))
    assert local_utc_offset_hours() == 6


def test_local_utc_offset_hours_negative_offset(mocker: MockerFixture) -> None:
    _mock_now(mocker, timedelta(hours=-8))
    assert local_utc_offset_hours() == -8


# Fixed-offset `Etc/GMT±N` zones (POSIX-inverted: `Etc/GMT-5` is UTC+5) never observe
# DST, so these are stable regardless of when the suite runs.


def test_utc_offset_hours_for_tz_positive_offset() -> None:
    assert utc_offset_hours_for_tz("Etc/GMT-5") == 5


def test_utc_offset_hours_for_tz_negative_offset() -> None:
    assert utc_offset_hours_for_tz("Etc/GMT+3") == -3


def test_utc_offset_hours_for_tz_unknown_zone_raises() -> None:
    with pytest.raises(ZoneInfoNotFoundError, match="not-a-real-zone"):
        utc_offset_hours_for_tz("not-a-real-zone")
