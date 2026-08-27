# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap/lib/docker_utils.py."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from agent_wrap.lib.docker_utils import (
    daemon_reachable,
    docker_run,
    get_container_uid,
    get_tty_args,
    get_user_args,
    host_network_build_args,
    image_claude_version,
    image_exists,
    inspect_containers,
    is_newer_version,
    is_rootless,
    is_wsl,
    latest_claude_version,
    list_container_names,
    parse_docker_timestamp,
    parse_mount_specs,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest_mock


@pytest.fixture(autouse=True)
def clear_rootless_cache() -> Iterator[None]:
    """``is_rootless`` is cached, so drop it around every case that mocks docker."""
    is_rootless.cache_clear()
    yield
    is_rootless.cache_clear()


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


def test_image_claude_version_returns_version_on_success(
    mocker: pytest_mock.MockFixture,
) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = (
        '{"dependencies":{"@anthropic-ai/claude-code":{"version":"2.0.50"}}}'
    )
    mock_run.return_value.returncode = 0
    assert image_claude_version("claude-agent") == "2.0.50"


def test_image_claude_version_returns_none_on_empty_stdout(
    mocker: pytest_mock.MockFixture,
) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 1
    assert image_claude_version("claude-agent") is None


def test_image_claude_version_tolerates_nonzero_rc_with_json(
    mocker: pytest_mock.MockFixture,
) -> None:
    """A dependency problem exits 1, but npm ls still prints the version JSON."""
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = (
        '{"dependencies":{"@anthropic-ai/claude-code":{"version":"2.0.50"}}}'
    )
    mock_run.return_value.returncode = 1
    assert image_claude_version("claude-agent") == "2.0.50"


def test_image_claude_version_returns_none_on_invalid_json(
    mocker: pytest_mock.MockFixture,
) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "not json"
    mock_run.return_value.returncode = 0
    assert image_claude_version("claude-agent") is None


# --- latest_claude_version ---


def test_latest_claude_version_returns_version_on_success(
    mocker: pytest_mock.MockFixture,
) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "2.0.51\n"
    mock_run.return_value.returncode = 0
    assert latest_claude_version("claude-agent") == "2.0.51"


def test_latest_claude_version_returns_none_on_empty_stdout(
    mocker: pytest_mock.MockFixture,
) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 1
    assert latest_claude_version("claude-agent") is None


def test_latest_claude_version_returns_none_on_timeout(
    mocker: pytest_mock.MockFixture,
) -> None:
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=15)
    assert latest_claude_version("claude-agent") is None


def test_latest_claude_version_uses_view_with_greater_timeout(
    mocker: pytest_mock.MockFixture,
) -> None:
    """A registry query reaches the network, so it gets more than the local 10s."""
    mock_run = mocker.patch("agent_wrap.lib.docker_utils.subprocess.run")
    mock_run.return_value.stdout = "2.0.51\n"
    mock_run.return_value.returncode = 0
    latest_claude_version("claude-agent")
    argv = mock_run.call_args[0][0]
    assert "view" in argv
    assert "@anthropic-ai/claude-code" in argv
    assert mock_run.call_args[1]["timeout"] == 15


# --- is_newer_version ---


def test_is_newer_version_true_when_latest_newer() -> None:
    assert is_newer_version("2.0.50", "2.0.51") is True


def test_is_newer_version_compares_numeric_parts() -> None:
    """2.0.10 must sort after 2.0.9, which a string compare gets wrong."""
    assert is_newer_version("2.0.9", "2.0.10") is True


def test_is_newer_version_false_when_same() -> None:
    assert is_newer_version("2.0.50", "2.0.50") is False


def test_is_newer_version_false_when_installed_newer() -> None:
    assert is_newer_version("2.0.51", "2.0.50") is False


def test_is_newer_version_false_on_none_inputs() -> None:
    assert is_newer_version(None, "2.0.51") is False
    assert is_newer_version("2.0.50", None) is False
    assert is_newer_version(None, None) is False


def test_is_newer_version_false_on_invalid_versions() -> None:
    assert is_newer_version("garbage", "2.0.51") is False
    assert is_newer_version("2.0.50", "not-a-version") is False
    assert is_newer_version("", "2.0.51") is False


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


def test_container_uid_zero_when_rootless(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_rootless", return_value=True, autospec=True)
    assert get_container_uid() == 0


def test_container_uid_is_host_uid_when_not_rootless(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("agent_wrap.lib.docker_utils.is_rootless", return_value=False, autospec=True)
    mocker.patch("os.getuid", return_value=1000)
    assert get_container_uid() == 1000


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


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["-v", "/srv/data:/data"], [("/srv/data", "/data", False)]),
        (["--volume", "/srv/models:/models:ro"], [("/srv/models", "/models", True)]),
        (["--volume=/srv/models:/models:ro,z"], [("/srv/models", "/models", True)]),
        (["-v", "./data:/data"], [("./data", "/data", False)]),
        (["-v", "../shared:/shared:ro"], [("../shared", "/shared", True)]),
        (["-v", "~/.cache/hf:/cache"], [("~/.cache/hf", "/cache", False)]),
        (["-v", "cache:/cache"], [(None, "/cache", False)]),
        (["-v", "/workspace/node_modules"], [(None, "/workspace/node_modules", False)]),
        (["--tmpfs", "/workspace/tmp"], [(None, "/workspace/tmp", False)]),
        (["--tmpfs=/workspace/tmp"], [(None, "/workspace/tmp", False)]),
        (
            ["--mount", "type=bind,source=/srv/data,target=/data"],
            [("/srv/data", "/data", False)],
        ),
        (
            ["--mount", "type=bind,src=./data,dst=/data,readonly"],
            [("./data", "/data", True)],
        ),
        (
            ["--mount=type=bind,source=/srv/data,destination=/data,ro=true"],
            [("/srv/data", "/data", True)],
        ),
        (
            ["--mount", "type=bind,source=/srv/data,target=/data,readonly=false"],
            [("/srv/data", "/data", False)],
        ),
        (["--mount", "type=volume,source=cache,target=/cache"], [(None, "/cache", False)]),
        (["--cap-add", "SYS_ADMIN", "--device", "/dev/fuse"], []),
        (["-v", "/a:/b:ro:extra"], []),
        (["--mount", "type=bind,source=/srv/data"], []),
        (["-v"], []),
        (
            ["--cap-add", "SYS_ADMIN", "-v", "/srv/data:/data", "-v", "/workspace/target"],
            [("/srv/data", "/data", False), (None, "/workspace/target", False)],
        ),
    ],
)
def test_parse_mount_specs(args: list[str], expected: list[tuple[str | None, str, bool]]) -> None:
    assert [tuple(spec) for spec in parse_mount_specs(args)] == expected
