# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/lib/docker_utils.py."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_wrap.lib.docker_utils import (
    docker_run,
    get_tty_args,
    get_user_args,
    host_network_build_args,
    image_exists,
    is_rootless,
    is_wsl,
)

if TYPE_CHECKING:
    import pytest_mock


def test_docker_run_returns_tuple(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "hello\n"
    mock_run.return_value.returncode = 0
    stdout, rc = docker_run("info")
    assert stdout == "hello"
    assert rc == 0


def test_docker_run_timeout(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)
    assert docker_run("info") == ("", 1)


def test_docker_run_file_not_found(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = FileNotFoundError()
    assert docker_run("info") == ("", 1)


def test_docker_run_check_true_raises(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "error"
    with pytest.raises(RuntimeError, match="docker info failed"):
        docker_run("info", check=True)


def test_rootless_true(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "Security Options: rootless"
    mock_run.return_value.returncode = 0
    assert is_rootless() is True


def test_rootless_false(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "Security Options: default"
    mock_run.return_value.returncode = 0
    assert is_rootless() is False


def test_rootless_timeout(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker info", timeout=10)
    assert is_rootless() is False


def test_rootless_file_not_found(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = FileNotFoundError()
    assert is_rootless() is False


def test_rootless_subprocess_error(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = subprocess.SubprocessError()
    assert is_rootless() is False


def test_image_exists_true(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.returncode = 0
    assert image_exists("claude-agent") is True


def test_image_exists_false(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.returncode = 1
    assert image_exists("claude-agent") is False


def test_image_exists_timeout(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=10)
    assert image_exists("missing") is False


def test_image_exists_file_not_found(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = FileNotFoundError()
    assert image_exists("missing") is False


def test_user_args_root_when_rootless(mocker: pytest_mock.MockFixture) -> None:
    # Rootless maps container-root to the host user, so pin to 0:0: this writes
    # bind mounts as the host user AND overrides any non-root USER baked into an
    # image (e.g. the Telegram sidecar) that would otherwise map to an
    # unprivileged subuid unable to write host-owned mounts.
    mocker.patch("agent_wrap.lib.docker_utils.is_rootless", return_value=True, autospec=True)
    assert get_user_args() == ["--user", "0:0"]


def test_user_args_returns_uid_gid_when_not_rootless(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_rootless", return_value=False, autospec=True)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch("os.getgid", return_value=1000)
    assert get_user_args() == ["--user", "1000:1000"]


def test_tty_args_interactive_when_stdin_is_tty(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.sys.stdin.isatty", return_value=True)
    assert get_tty_args() == ["-it"]


def test_tty_args_no_tty_when_stdin_not_terminal(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.sys.stdin.isatty", return_value=False)
    assert get_tty_args() == ["-i"]


def test_is_wsl_true(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.lib.docker_utils.Path", autospec=True)
    mock_path.return_value.read_text.return_value = "Linux version 5.15 (microsoft)"
    assert is_wsl() is True


def test_is_wsl_false(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.lib.docker_utils.Path", autospec=True)
    mock_path.return_value.read_text.return_value = "Linux version 5.15 (generic)"
    assert is_wsl() is False


def test_is_wsl_os_error(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.lib.docker_utils.Path", autospec=True)
    mock_path.return_value.read_text.side_effect = OSError("no file")
    assert is_wsl() is False


def test_host_network_build_args_on_wsl_with_env(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=True, autospec=True)
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    assert host_network_build_args() == ["--network", "host"]


def test_host_network_build_args_not_wsl(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=False, autospec=True)
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    assert host_network_build_args() == []


def test_host_network_build_args_env_unset(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=True, autospec=True)
    monkeypatch.delenv("AGENT_USE_HOST_NETWORK", raising=False)
    assert host_network_build_args() == []


def test_host_network_build_args_env_falsey(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=True, autospec=True)
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "0")
    assert host_network_build_args() == []
