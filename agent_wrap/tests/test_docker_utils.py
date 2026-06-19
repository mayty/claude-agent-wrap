# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/lib/docker_utils.py."""

from __future__ import annotations

import subprocess

import pytest
import pytest_mock

from agent_wrap.lib.docker_utils import (
    docker_run,
    get_tty_args,
    get_user_args,
    image_exists,
    is_rootless,
)

# --- docker_run ---


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


# --- is_rootless ---


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


# --- image_exists ---


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


# --- get_user_args ---


def test_user_args_empty_when_rootless(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_rootless", return_value=True)
    assert get_user_args() == []


def test_user_args_returns_uid_gid_when_not_rootless(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_rootless", return_value=False)
    mocker.patch("os.getuid", return_value=1000)
    mocker.patch("os.getgid", return_value=1000)
    assert get_user_args() == ["--user", "1000:1000"]


# --- get_tty_args ---


def test_tty_args_interactive_when_stdin_is_tty(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.sys.stdin.isatty", return_value=True)
    assert get_tty_args() == ["-it"]


def test_tty_args_no_tty_when_stdin_not_terminal(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.sys.stdin.isatty", return_value=False)
    assert get_tty_args() == ["-i"]
