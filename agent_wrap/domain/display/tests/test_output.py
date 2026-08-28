# This file has been created with the assistance of an AI tool.
"""Tests for DisplayService's basic stdout/stderr printers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.display.constants import ERROR_PREFIX, WARNING_PREFIX, Ansi
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    import pytest_mock

MESSAGE = "Error: '.claude-agent-wrap/startup.sh' exceeded its 10s timeout; aborting launch."


@pytest.fixture
def ds() -> DisplayService:
    """Return a real DisplayService — the printers write straight to the streams."""
    return DisplayService()


def test_error_is_tagged_and_red_on_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    ds.error(MESSAGE)
    # The tag sits inside the colour span, so it is red too.
    assert capsys.readouterr().err == f"{Ansi.BOLD_RED}{ERROR_PREFIX}{MESSAGE}{Ansi.RESET}\n"


def test_error_keeps_its_tag_off_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ds.error(MESSAGE)
    err = capsys.readouterr().err
    # Colour is stripped when redirected; the tag is what carries severity there.
    assert err == f"{ERROR_PREFIX}{MESSAGE}\n"
    assert "\033[" not in err


def test_warning_is_tagged_and_yellow_on_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    ds.warning(MESSAGE)
    assert capsys.readouterr().err == f"{Ansi.BOLD_YELLOW}{WARNING_PREFIX}{MESSAGE}{Ansi.RESET}\n"


def test_continuation_lines_align_under_the_error_tag(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ds.error("first thing went wrong\nthen do this\nand this")
    pad = " " * len(ERROR_PREFIX)
    assert capsys.readouterr().err == (
        f"{ERROR_PREFIX}first thing went wrong\n{pad}then do this\n{pad}and this\n"
    )


def test_continuation_lines_align_under_the_wider_warning_tag(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ds.warning("heads up\ndetail")
    # Alignment is computed from the tag width, so the wider tag indents further.
    assert capsys.readouterr().err == (
        f"{WARNING_PREFIX}heads up\n{' ' * len(WARNING_PREFIX)}detail\n"
    )
