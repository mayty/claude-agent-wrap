# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/lib/docker_utils.py."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_wrap.lib.docker_utils import (
    daemon_reachable,
    docker_run,
    get_tty_args,
    get_user_args,
    host_network_build_args,
    image_exists,
    inspect_containers,
    is_rootless,
    is_wsl,
    list_container_names,
    parse_docker_timestamp,
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


# --- daemon_reachable ---


def test_daemon_reachable_true(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "27.0.3"
    mock_run.return_value.returncode = 0
    assert daemon_reachable() is True


def test_daemon_reachable_false_when_docker_absent(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = FileNotFoundError()
    assert daemon_reachable() is False


# --- list_container_names ---


def test_list_container_names_parses_lines(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "agent-wrap-litellm-bedrock\nagent-wrap-telegram\n"
    mock_run.return_value.returncode = 0
    assert list_container_names("name=^agent-wrap-") == [
        "agent-wrap-litellm-bedrock",
        "agent-wrap-telegram",
    ]


def test_list_container_names_passes_every_filter(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 0
    list_container_names("label=a=b", "status=running")
    argv = mock_run.call_args[0][0]
    assert argv[:4] == ["docker", "ps", "-a", "--format"]
    assert argv.count("--filter") == 2
    assert "label=a=b" in argv
    assert "status=running" in argv


def test_list_container_names_includes_stopped(mocker: pytest_mock.MockFixture) -> None:
    """-a is required: an exited sidecar corpse is what a diagnostic listing wants."""
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 0
    list_container_names("name=^agent-wrap-")
    assert "-a" in mock_run.call_args[0][0]


def test_list_container_names_empty_on_failure(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 1
    assert list_container_names("name=x") == []


# --- inspect_containers ---


def test_inspect_containers_returns_lines_and_rc(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "row-a\nrow-b\n"
    mock_run.return_value.returncode = 0
    assert inspect_containers(["a", "b"], "{{.Name}}") == (["row-a", "row-b"], 0)


def test_inspect_containers_keeps_partial_rows_with_nonzero_rc(
    mocker: pytest_mock.MockFixture,
) -> None:
    """A container that vanished between listing and inspection is routine, not failure."""
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "row-a\nrow-b\n"
    mock_run.return_value.returncode = 1
    lines, rc = inspect_containers(["a", "b", "gone"], "{{.Name}}")
    assert lines == ["row-a", "row-b"]
    assert rc == 1


def test_inspect_containers_uses_container_subcommand(mocker: pytest_mock.MockFixture) -> None:
    """Plain `docker inspect` would fall back to matching an image of that name."""
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 0
    inspect_containers(["a"], "{{.Name}}")
    assert mock_run.call_args[0][0][:3] == ["docker", "container", "inspect"]


def test_inspect_containers_skips_docker_when_no_names(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    assert inspect_containers([], "{{.Name}}") == ([], 0)
    mock_run.assert_not_called()


def test_inspect_containers_empty_on_missing_docker(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = FileNotFoundError()
    assert inspect_containers(["a"], "{{.Name}}") == ([], 1)


# --- parse_docker_timestamp ---


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-30T09:39:12.123456789Z", "2026-07-30T09:39:12.123456+00:00"),
        ("2026-07-30T09:39:12.123456Z", "2026-07-30T09:39:12.123456+00:00"),
        ("2026-07-30T09:39:12.123Z", "2026-07-30T09:39:12.123000+00:00"),
        ("2026-07-30T09:39:12Z", "2026-07-30T09:39:12+00:00"),
        ("2026-07-30T09:39:12.123456789+00:00", "2026-07-30T09:39:12.123456+00:00"),
        ("2026-07-30T11:39:12+02:00", "2026-07-30T09:39:12+00:00"),
        ("2026-07-30T11:39:12+0200", "2026-07-30T09:39:12+00:00"),
        ("  2026-07-30T09:39:12Z  ", "2026-07-30T09:39:12+00:00"),
    ],
)
def test_parse_docker_timestamp_variants(raw: str, expected: str) -> None:
    parsed = parse_docker_timestamp(raw)
    assert parsed is not None
    assert parsed.isoformat() == expected


@pytest.mark.parametrize(
    "raw",
    [
        "0001-01-01T00:00:00Z",  # docker's "never" — would render a ~2000y uptime
        "",
        "garbage",
        "2026-07-30",
        "not-a-date-at-all",
    ],
)
def test_parse_docker_timestamp_unusable(raw: str) -> None:
    assert parse_docker_timestamp(raw) is None


def test_parse_docker_timestamp_truncates_rather_than_rounds() -> None:
    parsed = parse_docker_timestamp("2026-07-30T09:39:12.999999999Z")
    assert parsed is not None
    assert parsed.microsecond == 999999
