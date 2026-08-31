# This file has been created with the assistance of an AI tool.
"""Tests for the DisplayService spinner (public API only)."""

# This file has been edited with the assistance of an AI tool.
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import PollResult
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    import pytest_mock


@pytest.fixture
def ds() -> DisplayService:
    """Return a real DisplayService for spinner/poll tests."""
    return DisplayService()


def test_spin_while_runs_work_non_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ran = []
    ds.spin_while(
        label="my-op",
        message="doing…",
        done_message="done",
        work=lambda: ran.append(True),
    )
    assert ran == [True]
    assert "my-op: doing…" in capsys.readouterr().err


def test_spin_while_runs_work_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    ran = []
    ds.spin_while(
        label="my-op",
        message="doing…",
        done_message="done",
        work=lambda: ran.append(True),
    )
    assert ran == [True]
    assert "my-op: done" in capsys.readouterr().err


def test_spin_while_dynamic_message_non_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ds.spin_while(
        label="my-op",
        message=lambda: "computed",
        done_message="done",
        work=lambda: None,
    )
    assert "my-op: computed" in capsys.readouterr().err


def test_spin_while_done_message_none(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    ds.spin_while(
        label="my-op",
        message="doing…",
        done_message=lambda: None,
        work=lambda: None,
    )
    err = capsys.readouterr().err
    assert err.endswith("\n")
    assert "done" not in err


def _frozen_clock(mocker: pytest_mock.MockFixture) -> None:
    """Freeze time so the deadline never trips and sleeps are instant."""
    mocker.patch("time.monotonic", return_value=0.0)
    mocker.patch("time.sleep")


def test_poll_until_success_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    _frozen_clock(mocker)
    result = ds.poll_until(
        label="my-op",
        poll=lambda: (PollResult.SUCCESS, "healthy"),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is True
    err = capsys.readouterr().err
    assert "my-op: ready" in err
    assert "\033[2K" in err


def test_poll_until_failure_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    _frozen_clock(mocker)
    result = ds.poll_until(
        label="my-op",
        poll=lambda: (PollResult.FAILURE, "unhealthy"),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is False
    assert capsys.readouterr().err.endswith("\n")


def test_poll_until_pending_then_success(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    _frozen_clock(mocker)
    verdicts = iter([(PollResult.PENDING, "starting"), (PollResult.SUCCESS, "healthy")])
    result = ds.poll_until(
        label="my-op",
        poll=lambda: next(verdicts),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is True
    assert "my-op: ready" in capsys.readouterr().err


def test_poll_until_non_tty_prints_status_changes(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str], ds: DisplayService
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    _frozen_clock(mocker)
    verdicts = iter(
        [
            (PollResult.PENDING, "starting"),
            (PollResult.PENDING, "starting"),
            (PollResult.SUCCESS, "healthy"),
        ]
    )
    result = ds.poll_until(
        label="my-op",
        poll=lambda: next(verdicts),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is True
    err = capsys.readouterr().err
    assert err.count("my-op: starting") == 1
    assert "my-op: healthy" in err


def test_poll_until_timeout(mocker: pytest_mock.MockFixture, ds: DisplayService) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    mocker.patch("time.monotonic", side_effect=[0.0, 100.0])
    result = ds.poll_until(
        label="my-op",
        poll=lambda: (PollResult.PENDING, "starting"),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is False
