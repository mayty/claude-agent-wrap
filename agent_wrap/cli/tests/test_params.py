# This file has been created with the assistance of an AI tool.
"""Tests for the shared click parameter types."""

import re
from datetime import UTC, date, datetime

import click
import pytest

from agent_wrap.cli.params import DateSpec, Regex
from agent_wrap.containers import services

#: Frozen "now" for the relative-date cases; a Thursday, well away from a month boundary.
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)


@pytest.fixture
def frozen_now() -> None:
    """Pin ``StatsService.now_utc`` so ``-Nd`` offsets resolve deterministically."""
    services.stats_service.now_utc.return_value = NOW  # pyrefly: ignore [missing-attribute]


def test_date_spec_parses_absolute_iso_date() -> None:
    assert DateSpec().convert("2026-01-31", None, None) == date(2026, 1, 31)


@pytest.mark.usefixtures("frozen_now")
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("-0d", date(2026, 9, 3)),
        ("-1d", date(2026, 9, 2)),
        ("-14d", date(2026, 8, 20)),
        ("-365d", date(2025, 9, 3)),
    ],
)
def test_date_spec_parses_relative_offset(value: str, expected: date) -> None:
    assert DateSpec().convert(value, None, None) == expected


@pytest.mark.parametrize(
    "value",
    ["nope", "2026-02-30", "2026/01/01", "14d", "-14", "-14w", "", "2026-1-1x"],
)
def test_date_spec_rejects_malformed_value(value: str) -> None:
    with pytest.raises(click.BadParameter, match="expects YYYY-MM-DD or -Nd"):
        DateSpec().convert(value, None, None)


def test_date_spec_passes_through_an_already_converted_date() -> None:
    """Click runs convert() over defaults too, so a real date must survive untouched."""
    already = date(2026, 5, 5)
    assert DateSpec().convert(already, None, None) is already


def test_regex_compiles_a_valid_pattern() -> None:
    compiled = Regex().convert(r"^/home/\w+/work", None, None)
    assert compiled.pattern == r"^/home/\w+/work"


def test_regex_rejects_an_invalid_pattern() -> None:
    with pytest.raises(click.BadParameter, match="invalid regex pattern"):
        Regex().convert("[unclosed", None, None)


def test_regex_passes_through_an_already_compiled_pattern() -> None:
    already = re.compile("api")
    assert Regex().convert(already, None, None) is already
