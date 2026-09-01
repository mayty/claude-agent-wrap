# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.logs.service.LogsService."""

import os
import signal
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.constants import LOGS_DEFAULT_PORT
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.constants import LOG_FILE_NAME
from agent_wrap.domain.logs.daemon import read_state, state_dir, state_file, write_state
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.exceptions import LockTimeoutError

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


#: Pid the stubbed Popen reports for the viewer it "spawned".
VIEWER_PID = 5150


@pytest.fixture
def spawned_viewer(mocker: MockerFixture) -> Mock:
    """
    Run the fork-spawn path in-process, and yield the stubbed Popen.

    ``os.fork`` returning 0 puts the test on the intermediate's branch, which is where
    the viewer is started and the state file claimed. That branch ends in ``os._exit``,
    stubbed to a no-op so control returns to the test instead of leaving pytest -- which
    means ``os.waitpid`` has to be stubbed too, since execution then falls through to the
    parent's half with a pid of 0.
    """
    mocker.patch("agent_wrap.domain.logs.service.os.fork", return_value=0, autospec=True)
    mocker.patch("agent_wrap.domain.logs.service.os._exit", autospec=True)
    mocker.patch("agent_wrap.domain.logs.service.os.waitpid", autospec=True)
    popen = mocker.patch("agent_wrap.domain.logs.service.subprocess.Popen", autospec=True)
    popen.return_value.pid = VIEWER_PID
    return popen


def test_running_server_returns_state_when_alive(mocker: MockerFixture, logs_svc: LogsService):
    write_state(pid=4242, port=9001)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    state = logs_svc.running_server()
    assert state == {"pid": 4242, "port": 9001, "starting": False}


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


def test_viewer_state_reports_a_claimed_viewer_as_starting(
    mocker: MockerFixture, logs_svc: LogsService
) -> None:
    write_state(pid=4242, port=8765, starting=True)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    state = logs_svc.viewer_state()
    assert (state.running, state.starting) == (True, True)


def test_viewer_state_does_not_report_a_dead_claim_as_starting(
    mocker: MockerFixture, logs_svc: LogsService
) -> None:
    """A claim whose spawn never made it is "not running", not "coming up"."""
    write_state(pid=4242, port=8765, starting=True)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=False, autospec=True)
    state = logs_svc.viewer_state()
    assert (state.running, state.starting) == (False, False)


@pytest.mark.parametrize("starting", [False, True])
def test_autostart_adopts_an_existing_viewer_without_spawning(
    mocker: MockerFixture,
    logs_svc: LogsService,
    starting: bool,  # noqa: FBT001
) -> None:
    """Whether it is listening or still coming up, a claimed viewer is left alone."""
    write_state(pid=4242, port=8765, starting=starting)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    fork = mocker.patch("agent_wrap.domain.logs.service.os.fork", autospec=True)

    assert logs_svc.autostart() is True
    fork.assert_not_called()


def test_autostart_spawns_and_claims_the_state_file(
    logs_svc: LogsService, spawned_viewer: Mock
) -> None:
    assert logs_svc.autostart() is True
    assert read_state() == {"pid": VIEWER_PID, "port": LOGS_DEFAULT_PORT, "starting": True}
    argv = spawned_viewer.call_args.args[0]
    assert argv[1:] == ["-m", "agent_wrap", "logs", "--foreground", f"--port={LOGS_DEFAULT_PORT}"]


def test_autostart_detaches_the_viewer_into_its_own_session(
    logs_svc: LogsService, spawned_viewer: Mock
) -> None:
    logs_svc.autostart()
    assert spawned_viewer.call_args.kwargs["start_new_session"] is True


def test_autostart_leaves_the_viewer_no_inherited_fds(
    logs_svc: LogsService, spawned_viewer: Mock
) -> None:
    """
    An flock belongs to the open file description and survives fork, so a viewer that
    inherited the spawn lock's fd would hold that lock for its whole lifetime and wedge
    every later spawn. close_fds must stay at its default of True.
    """
    logs_svc.autostart()
    assert spawned_viewer.call_args.kwargs.get("close_fds", True) is True


def test_autostart_reports_failure_when_the_lock_is_unavailable(
    mocker: MockerFixture, logs_svc: LogsService
) -> None:
    mocker.patch(
        "agent_wrap.domain.logs.service.file_lock",
        side_effect=LockTimeoutError("held"),
        autospec=True,
    )
    assert logs_svc.autostart() is False


def test_autostart_reports_failure_when_the_fork_fails(
    mocker: MockerFixture, logs_svc: LogsService
) -> None:
    mocker.patch(
        "agent_wrap.domain.logs.service.os.fork",
        side_effect=OSError("cannot allocate memory"),
        autospec=True,
    )
    assert logs_svc.autostart() is False


def test_spawn_background_does_not_mistake_the_claim_for_readiness(
    mocker: MockerFixture, logs_svc: LogsService, spawned_viewer: Mock
) -> None:
    """The claim carries a pid but no listening socket, so it is not readiness."""
    assert spawned_viewer is not None
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    mocker.patch("agent_wrap.domain.logs.service.SPAWN_TIMEOUT_SEC", 0.01)
    killed: list[tuple[int, int]] = []
    mocker.patch(
        "agent_wrap.domain.logs.service.os.kill",
        lambda pid, sig: killed.append((pid, sig)),  # pyrefly: ignore [implicit-any-lambda]
    )
    logs_svc._display.spin_while.side_effect = lambda **kw: kw["work"]()  # pyrefly: ignore [missing-attribute]

    assert logs_svc.spawn_background(0) == 1
    logs_svc._display.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "logs viewer started but did not become ready in time"
    )
    assert (VIEWER_PID, signal.SIGTERM) in killed
    assert not state_file().exists()


def test_spawn_background_returns_at_once_when_it_adopts_a_listening_viewer(
    mocker: MockerFixture, logs_svc: LogsService
) -> None:
    write_state(pid=4242, port=9123, starting=False)
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    fork = mocker.patch("agent_wrap.domain.logs.service.os.fork", autospec=True)

    assert logs_svc.spawn_background(8765) == 0
    fork.assert_not_called()
    logs_svc._display.info.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        logs_svc.connect_line(9123)
    )
    logs_svc._display.spin_while.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_claim_or_spawn_forks_for_real_without_leaving_an_unreaped_child(
    mocker: MockerFixture, logs_svc: LogsService
) -> None:
    """
    Exercise the real fork/_exit/waitpid handshake, stubbing only the viewer itself.

    Only ``Popen`` is patched, so the intermediate process is genuinely forked, genuinely
    writes the claim, and genuinely ``os._exit``s -- and this process genuinely waits on
    it. The pid is set before the fork, so the intermediate's inherited copy of the stub
    reports the same int. Reaching the assertions at all proves ``waitpid`` returned
    rather than hanging; the last one proves nothing exited unreaped behind it.
    """
    popen = mocker.patch("agent_wrap.domain.logs.service.subprocess.Popen", autospec=True)
    popen.return_value.pid = VIEWER_PID

    first = logs_svc._claim_or_spawn(8765)
    assert first == {"pid": VIEWER_PID, "port": 8765, "starting": True}

    # A second caller adopts that claim instead of starting a second viewer.
    mocker.patch("agent_wrap.domain.logs.service.pid_alive", return_value=True, autospec=True)
    assert logs_svc._claim_or_spawn(8765) == first

    try:
        reaped = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return  # no children at all: the intermediate was reaped
    assert reaped[0] == 0, f"an exited child was left unreaped: {reaped}"
