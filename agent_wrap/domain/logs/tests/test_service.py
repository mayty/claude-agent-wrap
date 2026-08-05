# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.logs.service.LogsService."""

from __future__ import annotations

import signal
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.constants import LOG_FILE_NAME
from agent_wrap.domain.logs.daemon import state_dir, state_file, write_state
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from pytest_mock import MockerFixture


@pytest.fixture
def logs_svc() -> LogsService:
    """Return a LogsService with no-op pricing and stats dependencies."""
    return LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
        config_service=Mock(spec=ConfigService),
        display_service=Mock(spec=DisplayService),
    )


def test_running_server_returns_state_when_alive(mocker: MockerFixture, logs_svc: LogsService):
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    state = logs_svc.running_server()
    assert state == {"pid": 4242, "port": 9001}


def test_running_server_removes_stale_file_when_dead(mocker: MockerFixture, logs_svc: LogsService):
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=False, autospec=True)
    assert logs_svc.running_server() is None
    assert not state_file().exists()


def test_running_server_none_when_no_file(logs_svc: LogsService):
    assert logs_svc.running_server() is None


def test_stop_when_not_running(mocker: MockerFixture, logs_svc: LogsService):
    mocker.patch.object(logs_svc, "running_server", return_value=None, autospec=True)
    assert logs_svc.stop_daemon() == 0
    logs_svc._display.info.assert_any_call("no viewer is running")  # pyrefly: ignore [missing-attribute]


def test_stop_daemon_sends_sigterm_and_removes_state(mocker: MockerFixture, logs_svc: LogsService):
    write_state(pid=4242, port=9001)
    mocker.patch.object(
        logs_svc, "running_server", return_value={"pid": 4242, "port": 9001}, autospec=True
    )
    signals: list[tuple[int, int]] = []
    mocker.patch(
        "agent_wrap.domain.logs.service.os.kill",
        lambda pid, sig: signals.append((pid, sig)),  # pyrefly: ignore [implicit-any-lambda]
    )
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=False, autospec=True)
    assert logs_svc.stop_daemon() == 0
    assert (4242, signal.SIGTERM) in signals
    assert not state_file().exists()
    logs_svc._display.success.assert_any_call("Logs viewer stopped.")  # pyrefly: ignore [missing-attribute]


def test_stop_daemon_sends_sigkill_after_timeout(mocker: MockerFixture, logs_svc: LogsService):
    """After SIGTERM timeout, SIGKILL is sent and state is cleaned up."""
    write_state(pid=4242, port=9001)
    mocker.patch.object(
        logs_svc, "running_server", return_value={"pid": 4242, "port": 9001}, autospec=True
    )
    signals: list[tuple[int, int]] = []
    mocker.patch(
        "agent_wrap.domain.logs.service.os.kill",
        lambda pid, sig: signals.append((pid, sig)),  # pyrefly: ignore [implicit-any-lambda]
    )
    mocker.patch("agent_wrap.domain.logs.service.time.sleep")
    ticks = iter([0.0, 0.1, 0.2, 99.0, 99.0, 99.1])
    mocker.patch("agent_wrap.domain.logs.service.time.monotonic", side_effect=lambda: next(ticks))
    alive_calls = 0

    def _alive_side_effect(_pid: int) -> bool:
        nonlocal alive_calls
        alive_calls += 1
        return alive_calls <= 2

    mocker.patch(
        "agent_wrap.domain.logs.service.pid_alive", side_effect=_alive_side_effect, autospec=True
    )
    assert logs_svc.stop_daemon() == 0
    assert (4242, signal.SIGTERM) in signals
    assert (4242, signal.SIGKILL) in signals
    assert not state_file().exists()
    logs_svc._display.success.assert_any_call("Logs viewer stopped.")  # pyrefly: ignore [missing-attribute]


def test_stop_daemon_permission_error_propagates(mocker: MockerFixture, logs_svc: LogsService):
    """PermissionError from os.kill must propagate, not be silently swallowed."""
    write_state(pid=4242, port=9001)
    mocker.patch.object(
        logs_svc, "running_server", return_value={"pid": 4242, "port": 9001}, autospec=True
    )

    def _kill(_pid: int, _sig: int) -> None:
        raise PermissionError

    mocker.patch("agent_wrap.domain.logs.service.os.kill", _kill)

    with pytest.raises(PermissionError):
        logs_svc.stop_daemon()


# --- viewer_state (read-only counterpart of running_server) ---


def test_viewer_state_running_when_pid_alive(mocker: MockerFixture, logs_svc: LogsService) -> None:
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    state = logs_svc.viewer_state()
    assert state.running is True
    assert state.pid == 4242
    assert state.port == 9001


def test_viewer_state_keeps_stale_state_file(mocker: MockerFixture, logs_svc: LogsService) -> None:
    """running_server() unlinks it; a reporting read must not."""
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=False, autospec=True)
    state = logs_svc.viewer_state()
    assert state.running is False
    assert state.pid == 4242
    assert state_file().exists()


def test_viewer_state_not_running_without_state_file(logs_svc: LogsService) -> None:
    state = logs_svc.viewer_state()
    assert state.running is False
    assert state.pid is None
    assert state.port is None


def test_viewer_state_reports_logfile_size(mocker: MockerFixture, logs_svc: LogsService) -> None:
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    (state_dir() / LOG_FILE_NAME).write_text("x" * 128)
    state = logs_svc.viewer_state()
    assert state.log_size == 128
    assert state.log_mtime is not None


def test_viewer_state_logfile_absent(logs_svc: LogsService) -> None:
    state = logs_svc.viewer_state()
    assert state.log_size is None
    assert state.log_mtime is None
