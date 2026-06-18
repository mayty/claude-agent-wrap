# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/docker_utils.py."""

from __future__ import annotations

import subprocess

import pytest
import pytest_mock

from agent_wrap.lib.docker_utils import (
    count_labeled_containers,
    docker_run,
    get_user_args,
    image_exists,
    is_rootless,
    list_labeled_instance_ids,
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


# --- list_labeled_instance_ids / count_labeled_containers ---


def test_list_labeled_instance_ids_parses(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.docker_run")
    mock_run.return_value = ("inst-1\ninst-2\n", 0)
    ids = list_labeled_instance_ids({"agent-wrap.role": "claude-agent"})
    assert ids == ["inst-1", "inst-2"]


def test_list_labeled_instance_ids_drops_blank_lines(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.docker_run")
    mock_run.return_value = ("inst-1\n\n   \ninst-2", 0)
    assert list_labeled_instance_ids({"agent-wrap.role": "claude-agent"}) == ["inst-1", "inst-2"]


def test_list_labeled_instance_ids_docker_error(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.docker_run")
    mock_run.return_value = ("", 1)
    assert list_labeled_instance_ids({"agent-wrap.role": "claude-agent"}) == []


def test_list_labeled_instance_ids_builds_and_filters(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.docker_run")
    mock_run.return_value = ("", 0)
    list_labeled_instance_ids(
        {"agent-wrap.role": "claude-agent", "agent-wrap.sidecar": "litellm"},
        id_label="agent-wrap.instance-id",
    )
    args = mock_run.call_args.args
    assert "ps" in args
    assert "label=agent-wrap.role=claude-agent" in args
    assert "label=agent-wrap.sidecar=litellm" in args
    assert '{{.Label "agent-wrap.instance-id"}}' in args


def test_count_labeled_containers(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.docker_run")
    mock_run.return_value = ("inst-1\ninst-2\ninst-3\n", 0)
    assert count_labeled_containers({"agent-wrap.role": "claude-agent"}) == 3


def test_count_labeled_containers_zero_on_error(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.docker_run")
    mock_run.return_value = ("", 1)
    assert count_labeled_containers({"agent-wrap.role": "claude-agent"}) == 0
