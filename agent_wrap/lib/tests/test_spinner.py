# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/spinner.py."""

from __future__ import annotations

import pytest
import pytest_mock

from agent_wrap.lib.spinner import PollResult, Spinner


def test_frame_includes_label_and_glyph() -> None:
    frames = Spinner.SPINNERS["default"][0]
    line = Spinner("my-op")._frame(frames, 0, "working")
    assert "my-op: " in line
    assert frames[0] in line
    assert "\033[2K" in line  # erase-line, redrawn in place
    assert line.endswith("working")


def test_frame_cycles_glyphs() -> None:
    spin = Spinner("my-op")
    frames = Spinner.SPINNERS["default"][0]
    # Index wraps around the frame tuple.
    assert frames[0] in spin._frame(frames, len(frames), "x")


def test_final_clears_line() -> None:
    line = Spinner("my-op")._final("done")
    assert "\033[2K" in line
    assert line.endswith("my-op: done")


def test_choose_spinner_returns_frames_and_interval() -> None:
    frames, interval = Spinner("my-op")._choose_spinner()
    assert isinstance(frames, tuple)
    assert len(frames) > 0
    assert isinstance(interval, float)
    assert interval > 0


def test_spin_while_runs_work_non_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    ran = []
    Spinner("my-op").spin_while(
        message="doing…", done_message="done", work=lambda: ran.append(True)
    )
    assert ran == [True]
    assert "my-op: doing…" in capsys.readouterr().err


def test_spin_while_runs_work_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    ran = []
    # Trivial, instant work — the thread is joined before the call returns.
    Spinner("my-op").spin_while(
        message="doing…", done_message="done", work=lambda: ran.append(True)
    )
    assert ran == [True]
    assert "my-op: done" in capsys.readouterr().err


def test_spin_while_dynamic_message_non_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    Spinner("my-op").spin_while(message=lambda: "computed", done_message="done", work=lambda: None)
    assert "my-op: computed" in capsys.readouterr().err


def test_spin_while_done_message_none(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    Spinner("my-op").spin_while(message="doing…", done_message=lambda: None, work=lambda: None)
    err = capsys.readouterr().err
    # None finalize: the line is ended with a bare newline, no extra text.
    assert err.endswith("\n")
    assert "done" not in err


# --- poll_until ---


def _frozen_clock(mocker: pytest_mock.MockFixture) -> None:
    """Freeze time so the deadline never trips and sleeps are instant."""
    mocker.patch("time.monotonic", return_value=0.0)
    mocker.patch("time.sleep")


def test_poll_until_success_tty(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    _frozen_clock(mocker)
    result = Spinner("my-op").poll_until(
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
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    _frozen_clock(mocker)
    result = Spinner("my-op").poll_until(
        poll=lambda: (PollResult.FAILURE, "unhealthy"),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is False
    # Failure leaves the cursor on a fresh line (no 'ready' finalize).
    assert capsys.readouterr().err.endswith("\n")


def test_poll_until_pending_then_success(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.stderr.isatty", return_value=True)
    _frozen_clock(mocker)
    verdicts = iter([(PollResult.PENDING, "starting"), (PollResult.SUCCESS, "healthy")])
    result = Spinner("my-op").poll_until(
        poll=lambda: next(verdicts),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is True
    # Outcome only: with the poll loop on its own thread, whether a transient
    # frame is drawn before success is non-deterministic.
    assert "my-op: ready" in capsys.readouterr().err


def test_poll_until_non_tty_prints_status_changes(
    mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture[str]
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
    result = Spinner("my-op").poll_until(
        poll=lambda: next(verdicts),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is True
    err = capsys.readouterr().err
    # Each distinct status printed once; the repeat is suppressed.
    assert err.count("my-op: starting") == 1
    assert "my-op: healthy" in err


def test_poll_until_timeout(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("sys.stderr.isatty", return_value=False)
    # monotonic advances past the deadline so the loop exits on timeout.
    mocker.patch("time.monotonic", side_effect=[0.0, 100.0])
    result = Spinner("my-op").poll_until(
        poll=lambda: (PollResult.PENDING, "starting"),
        message="waiting",
        done_message="ready",
        timeout=10,
    )
    assert result is False
