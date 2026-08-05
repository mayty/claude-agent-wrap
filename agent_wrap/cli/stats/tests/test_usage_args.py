# This file has been created with the assistance of an AI tool.
"""
Tests for `agent stats` argument parsing.

Only the argparse layer lives here — flag spellings, the ``-Nd`` gluing that keeps
argparse from mistaking a relative date for an option, per-value validation, and how
a rejected window is reported. The resolution table itself belongs to
``StatsService.resolve_window`` and is tested in the stats domain.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import agent_wrap.cli.stats.usage_args as ua
from agent_wrap.cli.stats.usage_args import parse_usage_args
from agent_wrap.containers import services
from agent_wrap.domain.stats.models import WindowError

if TYPE_CHECKING:
    from unittest.mock import Mock

    from pytest_mock import MockerFixture

# A fixed "today" so relative offsets are deterministic.
_TODAY = date(2026, 6, 29)


def _parse(mocker: MockerFixture, display_mock: Mock, *flags: str):
    """Parse *flags*, with "today" frozen and the window resolver stubbed."""
    frozen = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 12, 0, 0, tzinfo=timezone.utc)
    services.stats_service.now_utc.return_value = frozen  # pyrefly: ignore [missing-attribute]
    # Pin the day boundary so a relative -Nd reduces to plain UTC-date arithmetic
    # regardless of the CI host's local offset.
    mocker.patch.object(ua, "DAY_START_HOURS", 0)
    services.stats_service.resolve_window.return_value = ("lo", "hi")  # pyrefly: ignore [missing-attribute]
    return parse_usage_args(list(flags), usage_line="u", usage_text="u", display=display_mock)


def _resolve_call() -> tuple[date | None, date | None, int | None, bool]:
    """Return the (from, until, days, days_given) the parser handed the resolver."""
    call = services.stats_service.resolve_window.call_args  # pyrefly: ignore [missing-attribute]
    return (*call.args, call.kwargs["days_given"])


def test_resolved_window_is_passed_through(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock)
    assert parsed is not None
    assert (parsed.from_iso, parsed.until_iso) == ("lo", "hi")
    assert parsed.verbose is False
    assert _resolve_call() == (None, None, None, False)


def test_absolute_dates_reach_the_resolver(mocker: MockerFixture, display_mock: Mock):
    _parse(mocker, display_mock, "--from", "2026-06-01", "--until", "2026-06-10")
    assert _resolve_call() == (date(2026, 6, 1), date(2026, 6, 10), None, False)


def test_short_flags_are_equivalent(mocker: MockerFixture, display_mock: Mock):
    _parse(mocker, display_mock, "-f", "2026-06-01", "-u", "2026-06-10", "-d", "5")
    assert _resolve_call() == (date(2026, 6, 1), date(2026, 6, 10), 5, True)


def test_relative_date_is_offset_from_today(mocker: MockerFixture, display_mock: Mock):
    """``--from -14d`` must survive argparse, which would read -14d as an option."""
    _parse(mocker, display_mock, "--from", "-14d")
    assert _resolve_call() == (_TODAY - timedelta(days=14), None, None, False)


def test_relative_until_is_offset_from_today(mocker: MockerFixture, display_mock: Mock):
    _parse(mocker, display_mock, "--until", "-7d", "--days", "3")
    assert _resolve_call() == (None, _TODAY - timedelta(days=7), 3, True)


def test_days_zero_is_given_not_absent(mocker: MockerFixture, display_mock: Mock):
    """``--days 0`` must reach the resolver as days_given, or it reads as "no flag"."""
    _parse(mocker, display_mock, "--days", "0")
    assert _resolve_call() == (None, None, 0, True)


def test_verbose_flag(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "-v")
    assert parsed is not None
    assert parsed.verbose is True


def test_refresh_flag(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "-r")
    assert parsed is not None
    assert parsed.refresh is True


def test_refresh_long_flag(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "--refresh")
    assert parsed is not None
    assert parsed.refresh is True


def test_refresh_defaults_false(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock)
    assert parsed is not None
    assert parsed.refresh is False


def test_window_error_is_reported_and_stops(mocker: MockerFixture, display_mock: Mock):
    frozen = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 12, 0, 0, tzinfo=timezone.utc)
    services.stats_service.now_utc.return_value = frozen  # pyrefly: ignore [missing-attribute]
    mocker.patch.object(ua, "DAY_START_HOURS", 0)
    services.stats_service.resolve_window.return_value = WindowError("nope")  # pyrefly: ignore [missing-attribute]
    parsed = parse_usage_args(["-d", "3"], usage_line="u", usage_text="u", display=display_mock)
    assert parsed is None
    display_mock.error.assert_called_once_with("nope")


def test_malformed_from_rejected(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "--from", "june-first")
    assert parsed is None
    display_mock.error.assert_not_called()  # argparse reported it itself


def test_negative_days_rejected(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "--days", "-3")
    assert parsed is None
    display_mock.error.assert_not_called()


def test_days_missing_value_rejected(mocker: MockerFixture, display_mock: Mock):
    """A bare --days must report its missing value rather than be silently absorbed."""
    parsed = _parse(mocker, display_mock, "--days")
    assert parsed is None
    display_mock.error.assert_not_called()


def test_unknown_flag_rejected(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "--nope")
    assert parsed is None


def test_positional_argument_rejected(mocker: MockerFixture, display_mock: Mock):
    """The registry path is derived, never passed — a stray positional is an error."""
    parsed = _parse(mocker, display_mock, "/some/projects.txt")
    assert parsed is None


def test_pattern_long_flag(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "--pattern", "api")
    assert parsed is not None
    assert parsed.pattern is not None
    assert parsed.pattern.pattern == "api"


def test_pattern_short_flag(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "-p", "^(proj-a|proj-b)$")
    assert parsed is not None
    assert parsed.pattern is not None
    assert parsed.pattern.pattern == "^(proj-a|proj-b)$"


def test_pattern_defaults_none(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock)
    assert parsed is not None
    assert parsed.pattern is None


def test_pattern_invalid_regex_rejected(mocker: MockerFixture, display_mock: Mock):
    parsed = _parse(mocker, display_mock, "-p", "[unclosed")
    assert parsed is None
    display_mock.error.assert_called_once()
