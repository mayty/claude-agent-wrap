# This file has been created with the assistance of an AI tool.
"""Tests for DisplayService's basic stdout/stderr printers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.display.constants import Ansi
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    import pytest_mock

MESSAGE = "Error: '.claude-agent-wrap/startup.sh' exceeded its 10s timeout; aborting launch."


@pytest.fixture
def ds() -> DisplayService:
    """Return a real DisplayService — the printers write straight to the streams."""
    return DisplayService()


def test_error_is_red_on_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    ds.error(MESSAGE)
    assert capsys.readouterr().err == f"{Ansi.BOLD_RED}{MESSAGE}{Ansi.RESET}\n"


def test_error_is_plain_off_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ds.error(MESSAGE)
    err = capsys.readouterr().err
    assert err == f"{MESSAGE}\n"
    assert "\033[" not in err
