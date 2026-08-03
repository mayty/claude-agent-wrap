# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for DisplayService's value formatters."""

from __future__ import annotations

import pytest

from agent_wrap.domain.display.service import DisplayService


@pytest.fixture
def display() -> DisplayService:
    """Return a real DisplayService — the formatters are pure functions."""
    return DisplayService()


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0B"),
        (1, "1B"),
        (1023, "1023B"),
        (1024, "1.0KB"),
        (1536, "1.5KB"),
        (1024 * 1024, "1.0MB"),
        (5 * 1024 * 1024 + 512 * 1024, "5.5MB"),
        (1024**3, "1.0GB"),
        (3 * 1024**4, "3072.0GB"),
    ],
)
def test_format_bytes(display: DisplayService, n: int, expected: str) -> None:
    assert display.format_bytes(n) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0K"),
        (1_500_000, "1.5M"),
        (2_000_000_000, "2.0G"),
    ],
)
def test_format_count(display: DisplayService, n: int, expected: str) -> None:
    assert display.format_count(n) == expected


@pytest.mark.parametrize(
    ("cost", "expected"),
    [(None, "?"), (0.0, "$0.00"), (1.005, "$1.00"), (12.349, "$12.35")],
)
def test_format_cost(display: DisplayService, cost: float | None, expected: str) -> None:
    assert display.format_cost(cost) == expected


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (None, "—"),
        (-1, "—"),
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m"),
        (90, "1m"),
        (3599, "59m"),
        (3600, "1h 0m"),
        (11520, "3h 12m"),
        (86399, "23h 59m"),
        (86400, "1d 0h"),
        (90000, "1d 1h"),
        (604800, "7d 0h"),
    ],
)
def test_format_duration(display: DisplayService, seconds: float | None, expected: str) -> None:
    assert display.format_duration(seconds) == expected


def test_format_duration_truncates_fractional_seconds(display: DisplayService) -> None:
    assert display.format_duration(59.9) == "59s"
