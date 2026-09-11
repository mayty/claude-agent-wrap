# This file has been edited with the assistance of an AI tool.
"""
CLI-layer tests for agent_wrap.cli.logs — argument parsing and calling protocol.

``services.logs_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from typing import TYPE_CHECKING

import click
import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli.logs.run import logs_command
from agent_wrap.constants import LOGS_DEFAULT_PORT
from agent_wrap.containers import services

if TYPE_CHECKING:
    from click.testing import CliRunner


def test_port_defaults_to_the_wrapper_default(runner: CliRunner) -> None:
    services.logs_service.running_server.return_value = None  # pyrefly: ignore [missing-attribute]
    services.logs_service.spawn_background.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["logs"])
    assert result.exit_code == 0
    services.logs_service.spawn_background.assert_called_once_with(LOGS_DEFAULT_PORT)  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("flag", ["--port", "-p"])
def test_port_is_forwarded(runner: CliRunner, flag: str) -> None:
    services.logs_service.running_server.return_value = None  # pyrefly: ignore [missing-attribute]
    services.logs_service.spawn_background.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["logs", flag, "9000"])
    assert result.exit_code == 0
    services.logs_service.spawn_background.assert_called_once_with(9000)  # pyrefly: ignore [missing-attribute]


def test_port_rejects_non_integer(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["logs", "--port", "abc"])
    assert result.exit_code == 2
    assert "is not a valid integer range" in result.output
    services.logs_service.spawn_background.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("bad", ["0", "70000", "-1"])
def test_port_rejects_out_of_range(runner: CliRunner, bad: str) -> None:
    result = runner.invoke(cli_root, ["logs", "--port", bad])
    assert result.exit_code == 2
    assert "is not in the range" in result.output
    services.logs_service.spawn_background.assert_not_called()  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_exits_zero(runner: CliRunner, flag: str) -> None:
    result = runner.invoke(cli_root, ["logs", flag])
    assert result.exit_code == 0
    assert "--port" in result.output


def test_unknown_flag_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["logs", "--bogus"])
    assert result.exit_code == 2
    assert "No such option '--bogus'" in result.output


@pytest.mark.parametrize("flag", ["--stop", "-s"])
def test_stop_dispatches_to_stop_daemon(runner: CliRunner, flag: str) -> None:
    services.logs_service.stop_daemon.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["logs", flag])
    assert result.exit_code == 0
    services.logs_service.stop_daemon.assert_called_once_with()  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("extra", [["--port", "9000"], ["--port", "8765"], ["--foreground"]])
def test_stop_rejects_every_other_argument(runner: CliRunner, extra: list[str]) -> None:
    """``--port 8765`` is rejected too: an explicit default is still an explicit argument."""
    result = runner.invoke(cli_root, ["logs", "--stop", *extra])
    assert result.exit_code == 1
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "agent logs --stop (takes no other arguments)"
    )
    services.logs_service.stop_daemon.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_foreground_dispatches_to_serve_foreground(runner: CliRunner) -> None:
    services.logs_service.serve_foreground.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["logs", "--foreground", "--port", "9000"])
    assert result.exit_code == 0
    services.logs_service.serve_foreground.assert_called_once_with(9000)  # pyrefly: ignore [missing-attribute]


def test_already_running_prints_connect_line_and_skips_spawn(runner: CliRunner) -> None:
    services.logs_service.running_server.return_value = {  # pyrefly: ignore [missing-attribute]
        "pid": 1,
        "port": 9123,
        "starting": False,
    }
    services.logs_service.connect_line.return_value = (  # pyrefly: ignore [missing-attribute]
        "LiteLLM log viewer running at http://127.0.0.1:9123"
    )

    result = runner.invoke(cli_root, ["logs", "--port", "8765"])
    assert result.exit_code == 0
    services.logs_service.spawn_background.assert_not_called()  # pyrefly: ignore [missing-attribute]
    services.display_service.info.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "LiteLLM log viewer running at http://127.0.0.1:9123"
    )


def test_starting_server_prints_starting_line_and_skips_spawn(runner: CliRunner) -> None:
    """A claimed-but-not-listening viewer is reported as starting, not started again."""
    services.logs_service.running_server.return_value = {  # pyrefly: ignore [missing-attribute]
        "pid": 1,
        "port": 9123,
        "starting": True,
    }
    services.logs_service.starting_line.return_value = "viewer is starting"  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["logs", "--port", "8765"])
    assert result.exit_code == 0
    services.logs_service.spawn_background.assert_not_called()  # pyrefly: ignore [missing-attribute]
    services.logs_service.connect_line.assert_not_called()  # pyrefly: ignore [missing-attribute]
    services.display_service.info.assert_called_once_with("viewer is starting")  # pyrefly: ignore [missing-attribute]


def test_forwards_spawn_exit_code(runner: CliRunner) -> None:
    services.logs_service.running_server.return_value = None  # pyrefly: ignore [missing-attribute]
    services.logs_service.spawn_background.return_value = 7  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["logs"])
    assert result.exit_code == 7


def test_foreground_flag_is_hidden_from_completion_and_help() -> None:
    """The re-exec'd child's flag must not be offered to users or documented."""
    ctx = click.Context(logs_command, info_name="logs")
    offered = [item.value for item in logs_command.shell_complete(ctx, "-")]
    assert "--port" in offered
    assert "--stop" in offered
    assert "--foreground" not in offered
    assert "--foreground" not in logs_command.get_help(ctx)


def test_logs_takes_no_registry_write_grant(runner: CliRunner, write_grants: list[str]) -> None:
    services.logs_service.running_server.return_value = None  # pyrefly: ignore [missing-attribute]
    services.logs_service.spawn_background.return_value = 0  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["logs"])

    assert write_grants == []


def test_the_viewer_daemon_takes_no_registry_write_grant(
    runner: CliRunner, write_grants: list[str]
) -> None:
    """
    ``--foreground`` *is* the viewer daemon, re-exec'd as its own process.

    It reads the registry on every reconcile, so a registry grant here would let a
    filesystem event migrate host state. This is the invocation the whole gate exists
    for, and it is now asserted per database rather than in the aggregate: the daemon
    does hold one grant, on the index it fills.
    """
    services.logs_service.serve_foreground.return_value = 0  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["logs", "--foreground"])

    assert "projects" not in write_grants


def test_the_viewer_daemon_takes_a_logs_write_grant(
    runner: CliRunner, write_grants: list[str]
) -> None:
    """
    The daemon is what keeps the request index current, so it must be able to write it.

    Nothing else on a normal host ingests. Without this grant every ingest pass is
    refused and every consumer's totals -- `agent stats`, the statusline, the viewer --
    freeze at whatever the last `agent reindex` saw, with no error anywhere to say so.
    """
    services.logs_service.serve_foreground.return_value = 0  # pyrefly: ignore [missing-attribute]

    runner.invoke(cli_root, ["logs", "--foreground"])

    assert write_grants == ["logs"]
