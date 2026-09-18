# This file has been edited with the assistance of an AI tool.
"""
CLI-layer tests for agent_wrap.cli.update — argument parsing and calling protocol.

``services.update_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from typing import TYPE_CHECKING

from agent_wrap.__main__ import cli_root
from agent_wrap.containers import services

if TYPE_CHECKING:
    from click.testing import CliRunner


def test_update_delegates_to_service(runner: CliRunner) -> None:
    services.update_service.apply.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["update"])
    assert result.exit_code == 0
    services.update_service.apply.assert_called_once_with()  # pyrefly: ignore [missing-attribute]


def test_update_forwards_service_error_code(runner: CliRunner) -> None:
    services.update_service.apply.return_value = 1  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["update"])
    assert result.exit_code == 1


def test_update_rejects_extra_args(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["update", "extra-arg"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output
    services.update_service.apply.assert_not_called()  # pyrefly: ignore [missing-attribute]
