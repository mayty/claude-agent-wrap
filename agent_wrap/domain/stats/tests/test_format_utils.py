# This file has been created with the assistance of an AI tool.
"""Tests for stats domain date/time formatting helpers."""

from __future__ import annotations

import pytest

from agent_wrap.domain.stats.format_utils import day_in_range


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
