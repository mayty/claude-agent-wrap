# This file has been edited with the assistance of an AI tool.
"""
CLI-layer tests for agent_wrap.cli.create — argument parsing and calling protocol.

``services.create_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from typing import TYPE_CHECKING

from agent_wrap.__main__ import cli_root
from agent_wrap.containers import services

if TYPE_CHECKING:
    from click.testing import CliRunner


def test_create_delegates_to_service(runner: CliRunner) -> None:
    """CLI entry point delegates to services.create_service.create()."""
    services.create_service.create.return_value = 0  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["create"])
    assert result.exit_code == 0
    services.create_service.create.assert_called_once_with()  # pyrefly: ignore [missing-attribute]


def test_create_forwards_service_error_code(runner: CliRunner) -> None:
    """Non-zero return from the service is forwarded to the caller."""
    services.create_service.create.return_value = 1  # pyrefly: ignore [missing-attribute]
    result = runner.invoke(cli_root, ["create"])
    assert result.exit_code == 1
    services.create_service.create.assert_called_once_with()  # pyrefly: ignore [missing-attribute]


def test_create_rejects_extra_args(runner: CliRunner) -> None:
    """Create accepts no arguments."""
    result = runner.invoke(cli_root, ["create", "extra-arg"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output
    services.create_service.create.assert_not_called()  # pyrefly: ignore [missing-attribute]
