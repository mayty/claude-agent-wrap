# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.startup.service.StartupService."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import time
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_BINARY_PATH,
    AGENT_STARTUP_SCRIPT_NAME,
    SIDECAR_NETWORK_NAME,
)
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.startup.constants import DEFAULT_STARTUP_RUNNER
from agent_wrap.domain.startup.service import StartupService
from agent_wrap.exceptions import StartupScriptError
from agent_wrap.lib.process_utils import pid_alive

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import MagicMock

    import pytest_mock

CHILD_PID = 4242
TREE_DEATH_TIMEOUT_SECONDS = 5.0


@pytest.fixture
def startup_svc(mocker: pytest_mock.MockFixture) -> StartupService:
    """Return a StartupService with a spec-mocked display."""
    return StartupService(display_service=mocker.Mock(spec=DisplayService))


@pytest.fixture
def write_script(tmp_path: Path) -> Callable[..., Path]:
    """Write a startup script into the project's asset directory and return its path."""

    def _write(content: str = "#!/bin/sh\nexit 0\n") -> Path:
        path = tmp_path / AGENT_ASSETS_DIR / AGENT_STARTUP_SCRIPT_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    return _write


@pytest.fixture
def patch_popen(mocker: pytest_mock.MockFixture) -> Callable[..., MagicMock]:
    """
    Patch ``subprocess.Popen`` and return the patch, so argv/env/kwargs can be asserted.

    ``wait_error`` is raised by the *first* ``wait()`` only -- the budget wait. The later
    calls are ``terminate_tree``'s reaping waits, which must see an exited process rather
    than the same exception again.
    """

    def _patch(returncode: int = 0, wait_error: BaseException | None = None) -> MagicMock:
        proc = mocker.Mock(spec=["pid", "wait"])
        proc.pid = CHILD_PID
        if wait_error is None:
            proc.wait.return_value = returncode
        else:
            proc.wait.side_effect = [wait_error, -signal.SIGTERM, -signal.SIGTERM]
        return mocker.patch(
            "agent_wrap.domain.startup.service.subprocess.Popen",
            autospec=True,
            return_value=proc,
        )

    return _patch


def test_run_succeeds_on_zero_exit(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
) -> None:
    write_script()
    popen = patch_popen()

    startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")

    popen.assert_called_once()


def test_run_executes_from_the_project_directory_with_stdin_closed(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
) -> None:
    script = write_script()
    popen = patch_popen()

    startup_svc.run(tmp_path, timeout=3.5, agent_name="proj", instance_id="proj-1")

    args, kwargs = popen.call_args
    assert args[0] == ["/bin/sh", str(script)]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert popen.return_value.wait.call_args.kwargs["timeout"] == 3.5


def test_run_starts_the_script_in_its_own_session(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
) -> None:
    """Without its own process group there is nothing for a timeout to signal."""
    write_script()
    popen = patch_popen()

    startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")

    assert popen.call_args.kwargs["start_new_session"] is True


def test_run_exports_the_agent_environment(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
) -> None:
    write_script()
    popen = patch_popen()

    startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")

    env = popen.call_args.kwargs["env"]
    assert env["AGENT_NAME"] == "proj"
    assert env["AGENT_INSTANCE_ID"] == "proj-1"
    assert env["AGENT_SIDECAR_NETWORK"] == SIDECAR_NETWORK_NAME
    assert env["AGENT_BINARY"] == str(AGENT_BINARY_PATH)
    # The host environment is inherited, not replaced.
    assert "PATH" in env


def test_run_aborts_on_non_zero_exit(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
) -> None:
    write_script()
    patch_popen(returncode=3)

    with pytest.raises(StartupScriptError, match="exit code 3"):
        startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")


def test_run_aborts_on_timeout(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
) -> None:
    write_script()
    patch_popen(wait_error=subprocess.TimeoutExpired(cmd="sh", timeout=10.0))

    with pytest.raises(StartupScriptError, match="exceeded its 10s timeout"):
        startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")


def test_run_signals_the_process_group_on_timeout(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
    mocker: pytest_mock.MockFixture,
) -> None:
    """SIGTERM first (the docker CLI turns it into a build cancel), then SIGKILL."""
    write_script()
    patch_popen(wait_error=subprocess.TimeoutExpired(cmd="sh", timeout=1.0))
    killpg = mocker.patch("agent_wrap.domain.startup.service.os.killpg")

    with pytest.raises(StartupScriptError):
        startup_svc.run(tmp_path, timeout=1.0, agent_name="proj", instance_id="proj-1")

    assert [call.args for call in killpg.call_args_list] == [
        (CHILD_PID, signal.SIGTERM),
        (CHILD_PID, signal.SIGKILL),
    ]


def test_run_relays_a_keyboard_interrupt_to_the_process_group(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
    mocker: pytest_mock.MockFixture,
) -> None:
    """The new session has no controlling tty, so Ctrl-C must be forwarded by hand."""
    write_script()
    patch_popen(wait_error=KeyboardInterrupt())
    killpg = mocker.patch("agent_wrap.domain.startup.service.os.killpg")

    with pytest.raises(StartupScriptError, match="was interrupted"):
        startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")

    assert killpg.call_count == 2


def test_run_tolerates_an_already_dead_process_group(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    patch_popen: Callable[..., MagicMock],
    mocker: pytest_mock.MockFixture,
) -> None:
    """A group that raced us to exit is not an error the user should ever see."""
    write_script()
    patch_popen(wait_error=subprocess.TimeoutExpired(cmd="sh", timeout=1.0))
    mocker.patch("agent_wrap.domain.startup.service.os.killpg", side_effect=ProcessLookupError)

    with pytest.raises(StartupScriptError, match="exceeded its"):
        startup_svc.run(tmp_path, timeout=1.0, agent_name="proj", instance_id="proj-1")


def test_run_aborts_when_the_interpreter_is_missing(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    mocker: pytest_mock.MockFixture,
) -> None:
    write_script("#!/nonexistent/interpreter\n")
    mocker.patch(
        "agent_wrap.domain.startup.service.subprocess.Popen",
        autospec=True,
        side_effect=FileNotFoundError(2, "No such file or directory"),
    )

    with pytest.raises(StartupScriptError, match="could not execute"):
        startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")


def test_run_warns_and_returns_when_the_script_is_missing(
    tmp_path: Path, startup_svc: StartupService, patch_popen: Callable[..., MagicMock]
) -> None:
    popen = patch_popen()

    startup_svc.run(tmp_path, timeout=10.0, agent_name="proj", instance_id="proj-1")

    popen.assert_not_called()
    startup_svc._display.warning.assert_called_once()  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("#!/bin/bash\nexit 0\n", ["/bin/bash"]),
        ("#!/usr/bin/env python3\n", ["/usr/bin/env", "python3"]),
        ("#!/usr/bin/env bash -e\n", ["/usr/bin/env", "bash", "-e"]),
        ("#!  /bin/dash  \nexit 0\n", ["/bin/dash"]),
        ("#!/bin/bash\r\nexit 0\r\n", ["/bin/bash"]),
        ("#!\nexit 0\n", [DEFAULT_STARTUP_RUNNER]),
        ("echo hi\n", [DEFAULT_STARTUP_RUNNER]),
        ("", [DEFAULT_STARTUP_RUNNER]),
    ],
)
def test_runner_argv_from_shebang(
    startup_svc: StartupService, tmp_path: Path, content: str, expected: list[str]
) -> None:
    script = tmp_path / AGENT_STARTUP_SCRIPT_NAME
    script.write_text(content)
    assert startup_svc.runner_argv(script) == expected


def test_runner_argv_tolerates_undecodable_bytes(
    startup_svc: StartupService, tmp_path: Path
) -> None:
    script = tmp_path / AGENT_STARTUP_SCRIPT_NAME
    script.write_bytes(b"\xff\xfe\x00binary\n")
    assert startup_svc.runner_argv(script) == [DEFAULT_STARTUP_RUNNER]


def test_runner_argv_falls_back_when_the_script_cannot_be_read(
    startup_svc: StartupService, tmp_path: Path
) -> None:
    assert startup_svc.runner_argv(tmp_path / "absent.sh") == [DEFAULT_STARTUP_RUNNER]


def test_runner_argv_ignores_the_execute_bit(startup_svc: StartupService, tmp_path: Path) -> None:
    """Exec bits are lost on many checkouts, so they must not gate execution."""
    script = tmp_path / AGENT_STARTUP_SCRIPT_NAME
    script.write_text("#!/bin/bash\nexit 0\n")
    script.chmod(0o644)
    assert startup_svc.runner_argv(script) == ["/bin/bash"]


@pytest.mark.parametrize("is_legacy", [False, True])
def test_warn_if_unused_warns_when_the_script_exists(
    tmp_path: Path,
    startup_svc: StartupService,
    write_script: Callable[..., Path],
    is_legacy: bool,  # noqa: FBT001
) -> None:
    write_script()

    startup_svc.warn_if_unused(tmp_path, is_legacy=is_legacy)

    message = startup_svc._display.warning.call_args.args[0]  # pyrefly: ignore [missing-attribute]
    assert AGENT_STARTUP_SCRIPT_NAME in message
    assert ("move" in message.lower()) is is_legacy


def test_warn_if_unused_is_silent_without_a_script(
    tmp_path: Path, startup_svc: StartupService
) -> None:
    startup_svc.warn_if_unused(tmp_path, is_legacy=False)
    startup_svc._display.warning.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_run_end_to_end_against_a_real_script(
    tmp_path: Path, startup_svc: StartupService, write_script: Callable[..., Path]
) -> None:
    """One unmocked run, so the argv/env/cwd contract is proven against a real shell."""
    marker = tmp_path / "ran"
    write_script(f'#!/bin/sh\necho "$AGENT_NAME" > "{marker}"\nexit 0\n')

    startup_svc.run(tmp_path, timeout=30.0, agent_name="proj", instance_id="proj-1")

    assert marker.read_text().strip() == "proj"


def test_run_end_to_end_propagates_a_real_failure(
    tmp_path: Path, startup_svc: StartupService, write_script: Callable[..., Path]
) -> None:
    write_script("#!/bin/sh\nexit 7\n")

    with pytest.raises(StartupScriptError, match="exit code 7"):
        startup_svc.run(tmp_path, timeout=30.0, agent_name="proj", instance_id="proj-1")


def test_run_kills_the_whole_process_tree_on_timeout(
    tmp_path: Path, startup_svc: StartupService, write_script: Callable[..., Path]
) -> None:
    """
    A grandchild must not outlive the budget.

    This is the orphaned-``docker build`` regression: ``subprocess.run(timeout=...)``
    SIGKILLs only the direct child, so a script that shelled out left the real work
    running -- still on the terminal, still on the docker daemon -- after the abort.
    """
    pidfile = tmp_path / "grandchild.pid"
    write_script(f'#!/bin/sh\nsleep 60 &\necho $! > "{pidfile}"\nwait\n')

    with pytest.raises(StartupScriptError, match="exceeded its"):
        startup_svc.run(tmp_path, timeout=1.0, agent_name="proj", instance_id="proj-1")

    grandchild = int(pidfile.read_text())
    try:
        deadline = time.monotonic() + TREE_DEATH_TIMEOUT_SECONDS
        while time.monotonic() < deadline and pid_alive(grandchild):
            time.sleep(0.05)
        assert not pid_alive(grandchild)
    finally:
        with contextlib.suppress(OSError):
            os.kill(grandchild, signal.SIGKILL)
