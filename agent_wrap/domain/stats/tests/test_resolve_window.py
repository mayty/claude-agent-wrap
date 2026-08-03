# This file has been created with the assistance of an AI tool.
"""Tests for the usage-window resolution table (--from/--until/--days semantics)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.constants import DEFAULT_DAYS
from agent_wrap.domain.stats.models import WindowError
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

# A fixed "today" so relative offsets and defaults are deterministic.
_TODAY = date(2026, 6, 29)


@pytest.fixture
def stats(mocker: MockerFixture) -> StatsService:
    """Return a StatsService whose "today" is pinned to _TODAY at plain UTC."""
    svc = StatsService(Mock(spec=PricingService), Mock(spec=ConfigService))
    frozen = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 12, 0, 0, tzinfo=timezone.utc)
    mocker.patch.object(svc, "now_utc", return_value=frozen, autospec=True)
    # Pin the day boundary to 0 so get_day() reduces to UTC-date extraction
    # regardless of the CI host's real local offset.
    mocker.patch("agent_wrap.domain.stats.service.DAY_START_HOURS", 0)
    return svc


def _iso(d: date) -> str:
    return d.isoformat()


def _resolve(
    stats: StatsService,
    from_date: date | None = None,
    until_date: date | None = None,
    days: int | None = None,
):
    return stats.resolve_window(from_date, until_date, days, days_given=days is not None)


def _bounds(
    stats: StatsService,
    from_date: date | None = None,
    until_date: date | None = None,
    days: int | None = None,
) -> tuple[str | None, str | None]:
    """Resolve, asserting success, so a caller can unpack the pair."""
    resolved = _resolve(stats, from_date, until_date, days)
    assert not isinstance(resolved, WindowError)
    return resolved


def test_no_bounds_defaults_to_last_28_days(stats: StatsService):
    assert _resolve(stats) == (_iso(_TODAY - timedelta(days=DEFAULT_DAYS - 1)), _iso(_TODAY))


def test_from_alone_runs_to_today(stats: StatsService):
    assert _resolve(stats, from_date=date(2026, 6, 1)) == ("2026-06-01", _iso(_TODAY))


def test_until_alone_spans_default_days(stats: StatsService):
    lo, hi = _bounds(stats, until_date=date(2026, 6, 20))
    assert hi == "2026-06-20"
    assert lo == _iso(date(2026, 6, 20) - timedelta(days=DEFAULT_DAYS - 1))


def test_days_alone(stats: StatsService):
    assert _resolve(stats, days=7) == (_iso(_TODAY - timedelta(days=6)), _iso(_TODAY))


def test_days_zero_is_all_time(stats: StatsService):
    # --days 0 lifts the count bound: open lower side, but the implicit upper stays
    # "now" (no --until given). Records carry timestamps <= now, so this is all-time.
    assert _resolve(stats, days=0) == (None, _iso(_TODAY))


def test_from_and_until_are_used_verbatim(stats: StatsService):
    resolved = _resolve(stats, from_date=date(2026, 6, 1), until_date=date(2026, 6, 10))
    assert resolved == ("2026-06-01", "2026-06-10")


def test_from_and_days_spans_forward(stats: StatsService):
    assert _resolve(stats, from_date=date(2026, 6, 1), days=5) == ("2026-06-01", "2026-06-05")


def test_from_and_days_zero_opens_upper(stats: StatsService):
    assert _resolve(stats, from_date=date(2026, 6, 1), days=0) == ("2026-06-01", None)


def test_until_and_days_spans_backward(stats: StatsService):
    assert _resolve(stats, until_date=date(2026, 6, 20), days=5) == ("2026-06-16", "2026-06-20")


def test_until_and_days_zero_opens_lower(stats: StatsService):
    assert _resolve(stats, until_date=date(2026, 6, 20), days=0) == (None, "2026-06-20")


def test_all_three_bounds_rejected(stats: StatsService):
    resolved = _resolve(stats, from_date=date(2026, 6, 1), until_date=date(2026, 6, 10), days=3)
    assert resolved == WindowError("usage: at most two of --from, --until, --days may be given")


def test_from_after_until_rejected(stats: StatsService):
    resolved = _resolve(stats, from_date=date(2026, 6, 10), until_date=date(2026, 6, 1))
    assert resolved == WindowError("usage: --from date is after --until date")


def test_equal_from_and_until_is_a_single_day(stats: StatsService):
    """Bounds are inclusive on both sides, so from == until selects that one day."""
    day = date(2026, 6, 10)
    assert _resolve(stats, from_date=day, until_date=day) == ("2026-06-10", "2026-06-10")
