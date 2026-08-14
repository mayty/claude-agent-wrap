# This file has been created with the assistance of an AI tool.
"""Tests for the AGENT_DAY_START_UTC parsing in the stats domain's constants module."""

from __future__ import annotations

from typing import TYPE_CHECKING
from zoneinfo import ZoneInfoNotFoundError

import pytest

from agent_wrap.constants import _parsed_day_start_hours

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_unset_env_var_defaults_to_negated_local_offset(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch("agent_wrap.constants.local_utc_offset_hours", return_value=3)
    assert _parsed_day_start_hours() == -3


@pytest.mark.parametrize("value", ["0", "5", "-5", "23", "-23"])
def test_valid_env_var_is_used_verbatim(mocker: MockerFixture, value: str) -> None:
    mocker.patch.dict("os.environ", {"AGENT_DAY_START_UTC": value}, clear=True)
    assert _parsed_day_start_hours() == int(value)


def test_malformed_env_var_raises(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"AGENT_DAY_START_UTC": "not-a-number"}, clear=True)
    with pytest.raises(ValueError, match="invalid literal"):
        _parsed_day_start_hours()


@pytest.mark.parametrize("value", ["24", "-24", "48"])
def test_out_of_range_env_var_raises(mocker: MockerFixture, value: str) -> None:
    mocker.patch.dict("os.environ", {"AGENT_DAY_START_UTC": value}, clear=True)
    with pytest.raises(ValueError, match="AGENT_DAY_START_UTC"):
        _parsed_day_start_hours()


def test_agent_timezone_used_when_day_start_utc_unset(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"AGENT_TIMEZONE": "Europe/Warsaw"}, clear=True)
    mocker.patch("agent_wrap.constants.utc_offset_hours_for_tz", return_value=2)
    assert _parsed_day_start_hours() == -2


def test_agent_day_start_utc_wins_over_agent_timezone(mocker: MockerFixture) -> None:
    mocker.patch.dict(
        "os.environ",
        {"AGENT_DAY_START_UTC": "4", "AGENT_TIMEZONE": "Europe/Warsaw"},
        clear=True,
    )
    tz_offset = mocker.patch("agent_wrap.constants.utc_offset_hours_for_tz")
    assert _parsed_day_start_hours() == 4
    tz_offset.assert_not_called()


def test_agent_timezone_unknown_zone_raises(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {"AGENT_TIMEZONE": "not-a-real-zone"}, clear=True)
    with pytest.raises(ZoneInfoNotFoundError):
        _parsed_day_start_hours()
