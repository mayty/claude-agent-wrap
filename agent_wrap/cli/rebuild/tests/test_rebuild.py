# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.rebuild — argument parsing and calling protocol."""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.containers import services

if TYPE_CHECKING:
    from click.testing import CliRunner


def test_rebuild_defaults_to_project_image(runner: CliRunner) -> None:
    services.build_service.rebuild.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["rebuild"])
    assert result.exit_code == 0
    services.build_service.rebuild.assert_called_once_with(full=False)  # pyrefly: ignore [missing-attribute]


@pytest.mark.parametrize("flag", ["--full", "-f"])
def test_rebuild_full_flag_forwards_true(runner: CliRunner, flag: str) -> None:
    services.build_service.rebuild.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["rebuild", flag])
    assert result.exit_code == 0
    services.build_service.rebuild.assert_called_once_with(full=True)  # pyrefly: ignore [missing-attribute]


def test_rebuild_forwards_service_exit_code(runner: CliRunner) -> None:
    services.build_service.rebuild.return_value = 3  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["rebuild"])
    assert result.exit_code == 3


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_rebuild_help_exits_zero(runner: CliRunner, flag: str) -> None:
    """``-h`` is declared on the root group, so it reaches every subcommand."""
    result = runner.invoke(cli_root, ["rebuild", flag])
    assert result.exit_code == 0
    assert "--full" in result.output
    services.build_service.rebuild.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_rebuild_unknown_flag_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["rebuild", "--bogus"])
    assert result.exit_code == 2
    assert "No such option '--bogus'" in result.output
    services.build_service.rebuild.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_rebuild_does_not_abbreviate_flags(runner: CliRunner) -> None:
    """Argparse ran with allow_abbrev=False; click never abbreviates, so --fu is an error."""
    result = runner.invoke(cli_root, ["rebuild", "--fu"])
    assert result.exit_code == 2
    services.build_service.rebuild.assert_not_called()  # pyrefly: ignore [missing-attribute]
