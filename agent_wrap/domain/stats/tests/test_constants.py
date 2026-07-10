# This file has been created with the assistance of an AI tool.
"""Tests for the AGENT_DAY_START_UTC parsing in the stats domain's constants module."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.stats.constants import _parsed_day_start_hours

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


def test_unset_env_var_defaults_to_negated_local_offset(mocker: MockerFixture) -> None:
    mocker.patch.dict("os.environ", {}, clear=True)
    mocker.patch("agent_wrap.domain.stats.constants.local_utc_offset_hours", return_value=3)
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
