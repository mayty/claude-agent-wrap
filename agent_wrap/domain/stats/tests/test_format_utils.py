# This file has been created with the assistance of an AI tool.
"""Tests for stats domain date/time helpers — the day filter and the window bounds."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import DAY_START_HOURS
from agent_wrap.domain.stats.constants import HOURS_PER_DAY, SECONDS_PER_HOUR
from agent_wrap.domain.stats.fold import hour_bucket_dt
from agent_wrap.domain.stats.format_utils import day_in_range, hour_bounds
from agent_wrap.lib.daytime import get_day

if TYPE_CHECKING:
    import pytest_mock


@pytest.mark.parametrize(
    ("date_str", "from_date", "until_date"),
    [
        ("2026-06-01", "2026-06-01", "2026-06-10"),
        ("2026-06-10", "2026-06-01", "2026-06-10"),
    ],
)
def test_day_in_range_inclusive_bounds(date_str: str, from_date: str, until_date: str) -> None:
    assert day_in_range(date_str, from_date, until_date) is True


@pytest.mark.parametrize(
    ("date_str", "from_date", "until_date"),
    [
        ("2026-05-31", "2026-06-01", "2026-06-10"),
        ("2026-06-11", "2026-06-01", "2026-06-10"),
    ],
)
def test_day_in_range_out_of_bounds(date_str: str, from_date: str, until_date: str) -> None:
    assert day_in_range(date_str, from_date, until_date) is False


def test_day_in_range_open_lower_bound() -> None:
    assert day_in_range("2000-01-01", None, "2026-06-10") is True


def test_day_in_range_open_upper_bound() -> None:
    assert day_in_range("2030-01-01", "2026-06-01", None) is True


def test_day_in_range_fully_unbounded() -> None:
    assert day_in_range("2026-06-05", None, None) is True


@pytest.mark.parametrize(
    ("date_str", "from_date", "until_date", "expected"),
    [
        ("?", None, None, True),
        ("?", "2026-06-01", None, False),
        ("?", None, "2026-06-10", False),
    ],
)
def test_day_in_range_question_mark_sentinel(
    date_str: str,
    from_date: str | None,
    until_date: str | None,
    expected: bool,  # noqa: FBT001
) -> None:
    assert day_in_range(date_str, from_date, until_date) is expected


def _day_of(hour_bucket: int) -> str:
    """Return the stats day an hour bucket falls in, the way the fold derives it."""
    return get_day(hour_bucket_dt(hour_bucket), DAY_START_HOURS).isoformat()


def test_an_open_side_stays_open() -> None:
    assert hour_bounds(None, None) == (None, None)


@pytest.mark.parametrize("day", ["2026-06-05", "2026-01-01", "2026-12-31", "2024-02-29"])
def test_the_bounds_span_exactly_the_day_they_name(day: str) -> None:
    """
    Every hour in ``[lo, hi]`` belongs to *day*, and the hours either side do not.

    Exact rather than widened, because ``DAY_START_HOURS`` is a whole number of hours so
    a day boundary always falls on an hour boundary. Asserted by deriving each bound's
    day back through the fold's own conversion, so the two directions cannot drift apart
    — which is the failure that would silently move a day's spend into its neighbour.
    """
    lo, hi = hour_bounds(day, day)
    assert lo is not None
    assert hi is not None

    assert hi - lo == HOURS_PER_DAY - 1
    assert _day_of(lo) == day
    assert _day_of(hi) == day
    assert _day_of(lo - 1) < day
    assert _day_of(hi + 1) > day


def test_a_multi_day_window_covers_every_hour_between() -> None:
    lo, hi = hour_bounds("2026-06-05", "2026-06-07")
    assert lo is not None
    assert hi is not None

    assert hi - lo == 3 * HOURS_PER_DAY - 1
    assert _day_of(lo) == "2026-06-05"
    assert _day_of(hi) == "2026-06-07"


@pytest.mark.parametrize("day_start", [-11, -1, 0, 1, 5, 13])
def test_the_bounds_follow_the_configured_day_start(
    mocker: pytest_mock.MockerFixture, day_start: int
) -> None:
    """
    ``AGENT_DAY_START_UTC`` shifts a day's span, and the bounds have to shift with it.

    Both modules read the constant at import, so both are patched — the point is that
    the SQL bound and the Python day rule agree under any offset, and a test that moved
    only one of them would pass while the pair disagreed.
    """
    mocker.patch("agent_wrap.domain.stats.format_utils.DAY_START_HOURS", day_start)
    mocker.patch("agent_wrap.domain.stats.fold.DAY_START_HOURS", day_start)

    lo, hi = hour_bounds("2026-06-05", "2026-06-05")
    assert lo is not None
    assert hi is not None

    assert get_day(hour_bucket_dt(lo), day_start).isoformat() == "2026-06-05"
    assert get_day(hour_bucket_dt(hi), day_start).isoformat() == "2026-06-05"
    assert get_day(hour_bucket_dt(lo - 1), day_start).isoformat() == "2026-06-04"
    assert get_day(hour_bucket_dt(hi + 1), day_start).isoformat() == "2026-06-06"


def test_an_hour_bucket_round_trips_to_the_hour_it_names() -> None:
    """The inverse of the index's own expression, so the two cannot disagree."""
    dt = datetime(2026, 6, 5, 17, tzinfo=UTC)
    bucket = int(dt.timestamp()) // SECONDS_PER_HOUR

    assert hour_bucket_dt(bucket) == dt
