# This file has been created with the assistance of an AI tool.
"""Tests for DisplayService's basic stdout/stderr printers."""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.display.constants import ERROR_PREFIX, WARNING_PREFIX
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    import pytest_mock

# The SGR sequences the console emits for the palette, spelled out rather than derived
# from `Style`: these are what a terminal actually receives, and a test that recomputed
# them from the same names the code uses could not tell a renamed style from a wrong one.
RED = "\033[1;31m"
YELLOW = "\033[1;33m"
PURPLE = "\033[35m"
RESET = "\033[0m"

MESSAGE = "Error: '.claude-agent-wrap/startup.sh' exceeded its 10s timeout; aborting launch."
BANNER_TEXT = "Agent instance: 3f9a1c"


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
    assert capsys.readouterr().err == f"{RED}{ERROR_PREFIX}{MESSAGE}{RESET}\n"


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
    assert capsys.readouterr().err == f"{YELLOW}{WARNING_PREFIX}{MESSAGE}{RESET}\n"


def test_alert_carries_the_warning_tag_in_red_on_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    """The loud warning: `warning`'s tag, `error`'s colour."""
    mocker.patch("sys.stderr.isatty", return_value=True)
    ds.alert(MESSAGE)
    assert capsys.readouterr().err == f"{RED}{WARNING_PREFIX}{MESSAGE}{RESET}\n"


def test_alert_keeps_its_tag_off_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ds.alert(MESSAGE)
    err = capsys.readouterr().err
    assert err == f"{WARNING_PREFIX}{MESSAGE}\n"
    assert "\033[" not in err


def test_banner_is_marked_and_purple_on_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stdout.isatty", return_value=True)
    ds.banner(BANNER_TEXT)
    assert capsys.readouterr().out == f"{PURPLE}> {BANNER_TEXT}{RESET}\n"


def test_banner_keeps_its_marker_off_a_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stdout.isatty", return_value=False)
    ds.banner(BANNER_TEXT)
    out = capsys.readouterr().out
    # Colour is stripped when redirected; the marker is what still sets a banner apart.
    assert out == f"> {BANNER_TEXT}\n"
    assert "\033[" not in out


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
