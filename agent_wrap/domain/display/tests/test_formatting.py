# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for DisplayService's value formatters."""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0 Bytes"),
        (1, "1 Byte"),
        (1023, "1023 Bytes"),
        (1024, "1.0 KiB"),
        (1536, "1.5 KiB"),
        (1024 * 1024, "1.0 MiB"),
        (5 * 1024 * 1024 + 512 * 1024, "5.5 MiB"),
        (1024**3, "1.0 GiB"),
        (3 * 1024**4, "3.0 TiB"),
        (2 * 1024**5, "2.0 PiB"),
    ],
)
def test_format_bytes(non_tty_display: DisplayService, n: int, expected: str) -> None:
    assert non_tty_display.format_bytes(n) == expected


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0K"),
        (1_500_000, "1.5M"),
        (2_000_000_000, "2.0G"),
        (5 * 10**15, "5.0P"),
    ],
)
def test_format_count(non_tty_display: DisplayService, n: int, expected: str) -> None:
    assert non_tty_display.format_count(n) == expected


@pytest.mark.parametrize(
    ("cost", "expected"),
    [(None, "?"), (0.0, "$0.00"), (1.005, "$1.00"), (12.349, "$12.35")],
)
def test_format_cost(non_tty_display: DisplayService, cost: float | None, expected: str) -> None:
    assert non_tty_display.format_cost(cost) == expected


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
def test_format_duration(
    non_tty_display: DisplayService, seconds: float | None, expected: str
) -> None:
    assert non_tty_display.format_duration(seconds) == expected


def test_format_duration_truncates_fractional_seconds(non_tty_display: DisplayService) -> None:
    assert non_tty_display.format_duration(59.9) == "59s"
