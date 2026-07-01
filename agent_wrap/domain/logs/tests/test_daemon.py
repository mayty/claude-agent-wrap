from __future__ import annotations

import json
import signal
from pathlib import Path
from unittest.mock import Mock

import pytest
from pytest_mock import MockerFixture

from agent_wrap.cli import logs as logs_mod
from agent_wrap.domain.logs.daemon import (
    read_state,
    state_file,
    write_state,
)
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.lib.process_utils import pid_alive


@pytest.fixture
def logs_svc() -> LogsService:
    """Return a LogsService with no-op pricing and stats dependencies."""
    return LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
    )


# --- background server: state file + liveness ------------------------------


def test_state_file_path(tmp_path: Path):
    assert state_file() == tmp_path / ".agent-launches" / "logs-server.json"


def test_read_state_missing_returns_none(tmp_path: Path):
    assert read_state() is None


def test_read_state_corrupt_returns_none(tmp_path: Path):
    (tmp_path / ".agent-launches").mkdir()
    state_file().write_text("not json {{{", encoding="utf-8")
    assert read_state() is None


def test_read_state_rejects_wrong_shape(tmp_path: Path):
    (tmp_path / ".agent-launches").mkdir()
    # Missing/wrong-typed pid and port must be rejected.
    state_file().write_text(json.dumps({"pid": "x", "port": 8765}), encoding="utf-8")
    assert read_state() is None


def test_write_thenread_state_round_trip(tmp_path: Path):
    write_state(pid=4242, port=8765)
    state = read_state()
    assert state == {"pid": 4242, "port": 8765}


def test_pid_alive_true_for_running(mocker: MockerFixture):
    mocker.patch.object(logs_mod.os, "kill", return_value=None)
    assert pid_alive(123) is True


def test_pid_alive_false_for_dead(mocker: MockerFixture):
    def _kill(pid: int, sig: int):
        raise ProcessLookupError

    mocker.patch.object(logs_mod.os, "kill", _kill)
    assert pid_alive(123) is False


def test_pid_alive_true_for_permission_error(mocker: MockerFixture):
    def _kill(pid: int, sig: int):
        raise PermissionError

    mocker.patch.object(logs_mod.os, "kill", _kill)
    assert pid_alive(123) is True


def test_running_server_returns_state_when_alive(
    tmp_path: Path, mocker: MockerFixture, logs_svc: LogsService
):
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True)
    state = logs_svc.running_server()
    assert state == {"pid": 4242, "port": 9001}


def test_running_server_removes_stale_file_when_dead(
    tmp_path: Path, mocker: MockerFixture, logs_svc: LogsService
):
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=False)
    assert logs_svc.running_server() is None
    assert not state_file().exists()


def test_running_server_none_when_no_file(tmp_path: Path, logs_svc: LogsService):
    assert logs_svc.running_server() is None


# --- background server: stop_daemon ----------------------------------------------


def test_stop_when_not_running(
    tmp_path: Path, mocker: MockerFixture, capsys: pytest.CaptureFixture[str], logs_svc: LogsService
):
    mocker.patch.object(logs_svc, "running_server", return_value=None)
    assert logs_svc.stop_daemon() == 0
    assert "no viewer is running" in capsys.readouterr().out


def test_stop_daemon_sends_sigterm_and_removes_state(
    tmp_path: Path, mocker: MockerFixture, capsys: pytest.CaptureFixture[str], logs_svc: LogsService
):
    write_state(pid=4242, port=9001)
    mocker.patch.object(logs_svc, "running_server", return_value={"pid": 4242, "port": 9001})
    signals: list[tuple[int, int]] = []
    mocker.patch(
        "agent_wrap.domain.logs.service.os.kill", lambda pid, sig: signals.append((pid, sig))
    )
    # Report the PID as dead immediately so stop_daemon doesn't spin on the wait loop.
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=False)
    assert logs_svc.stop_daemon() == 0
    assert (4242, signal.SIGTERM) in signals
    assert not state_file().exists()
    assert "viewer stopped" in capsys.readouterr().out


def test_stop_daemon_sends_sigkill_after_timeout(
    tmp_path: Path, mocker: MockerFixture, capsys: pytest.CaptureFixture[str], logs_svc: LogsService
):
    """After SIGTERM timeout, SIGKILL is sent and state is cleaned up."""
    write_state(pid=4242, port=9001)
    mocker.patch.object(logs_svc, "running_server", return_value={"pid": 4242, "port": 9001})
    signals: list[tuple[int, int]] = []
    mocker.patch(
        "agent_wrap.domain.logs.service.os.kill", lambda pid, sig: signals.append((pid, sig))
    )
    mocker.patch("agent_wrap.domain.logs.service.time.sleep")
    # Simulate time: stay under the deadline for two loop iterations, then jump
    # far past it so the SIGTERM loop exits.  After SIGKILL, advance one more
    # tick so the post-SIGKILL loop can check pid_alive.
    ticks = iter([0.0, 0.1, 0.2, 99.0, 99.0, 99.1])
    mocker.patch("agent_wrap.domain.logs.service.time.monotonic", side_effect=lambda: next(ticks))
    # pid_alive: True during SIGTERM phase (process survives SIGTERM),
    # then False after SIGKILL (process killed).
    alive_calls = 0

    def _alive_side_effect(_pid: int) -> bool:
        nonlocal alive_calls
        alive_calls += 1
        return alive_calls <= 2  # Survives two SIGTERM-phase checks, then dies

    mocker.patch("agent_wrap.domain.logs.service.pid_alive", side_effect=_alive_side_effect)
    assert logs_svc.stop_daemon() == 0
    assert (4242, signal.SIGTERM) in signals
    assert (4242, signal.SIGKILL) in signals
    assert not state_file().exists()
    assert "viewer stopped" in capsys.readouterr().out
