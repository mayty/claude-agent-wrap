# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.secrets — argument parsing and calling protocol."""

from typing import TYPE_CHECKING

import click
import pytest

from agent_wrap.__main__ import cli_root
from agent_wrap.cli.secrets.run import secrets_group
from agent_wrap.constants import TELEGRAM_SIDECAR_NAME
from agent_wrap.containers import services
from agent_wrap.domain.secrets.models import SecretsCheckReport, SecretsSetResult

if TYPE_CHECKING:
    from click.testing import CliRunner
    from pytest_mock import MockerFixture

#: The four subcommands the group must expose, and nothing else.
SUBCOMMANDS = ("check", "set", "clear", "cleanup")


def test_group_exposes_exactly_the_four_subcommands() -> None:
    assert set(secrets_group.commands) == set(SUBCOMMANDS)


def test_bare_secrets_is_a_usage_error(runner: CliRunner) -> None:
    """A group invoked with no subcommand prints its help and exits 2."""
    result = runner.invoke(cli_root, ["secrets"])
    assert result.exit_code == 2
    for name in SUBCOMMANDS:
        assert name in result.output


def test_unknown_subcommand_is_a_usage_error(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["secrets", "bogus"])
    assert result.exit_code == 2
    assert "No such command 'bogus'" in result.output


def test_mistyped_subcommand_is_suggested(runner: CliRunner) -> None:
    """Click suggests near-misses, which argparse's suggest_on_error did for choices=."""
    result = runner.invoke(cli_root, ["secrets", "chek"])
    assert result.exit_code == 2
    assert "Did you mean 'check'?" in result.output


@pytest.mark.parametrize("action", ["check", "set", "clear"])
def test_sidecar_is_required(runner: CliRunner, action: str) -> None:
    """Click enforces the arity these three share; no hand-rolled check remains."""
    result = runner.invoke(cli_root, ["secrets", action])
    assert result.exit_code == 2
    assert "Missing argument 'SIDECAR'" in result.output


def test_cleanup_takes_no_sidecar(runner: CliRunner) -> None:
    result = runner.invoke(cli_root, ["secrets", "cleanup", "extra"])
    assert result.exit_code == 2
    assert "Got unexpected extra argument" in result.output
    services.secrets_service.cleanup_secrets.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_check_reports_all_present(runner: CliRunner) -> None:
    """'check telegram' returns 0 when the report says every secret is present."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:TelegramBotToken": True, "telegram:TelegramChatId": True},
        all_present=True,
        declares_none=False,
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    services.secrets_service.check_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]


def test_check_missing_secret_returns_one(runner: CliRunner) -> None:
    """'check' returns 1 when the report's verdict is not all-present."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:Token": False}, all_present=False, declares_none=False
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 1
    services.display_service.error.assert_called_once()  # pyrefly: ignore [missing-attribute]


def test_check_declares_no_secrets_returns_zero(runner: CliRunner) -> None:
    """A sidecar requiring nothing is reported as such, not as an empty pass."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={}, all_present=True, declares_none=True
    )

    result = runner.invoke(cli_root, ["secrets", "check", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    assert "declares no secrets" in services.display_service.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]


def test_set_non_interactive_returns_one(runner: CliRunner) -> None:
    """'set' reports the result's error and fails when there is no TTY."""
    services.secrets_service.set_secrets.return_value = SecretsSetResult(  # pyrefly: ignore [missing-attribute]
        keys_set=[], error="Cannot prompt for secrets in a non-interactive session."
    )

    result = runner.invoke(cli_root, ["secrets", "set", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 1
    services.secrets_service.set_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "Cannot prompt for secrets in a non-interactive session."
    )


def test_set_succeeds_when_keys_were_set(runner: CliRunner) -> None:
    services.secrets_service.set_secrets.return_value = SecretsSetResult(  # pyrefly: ignore [missing-attribute]
        keys_set=["telegram:Token"]
    )

    result = runner.invoke(cli_root, ["secrets", "set", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    services.display_service.error.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_clear_removes_namespaced_keys(runner: CliRunner) -> None:
    """'clear telegram' delegates to secrets_service.clear_secrets."""
    services.secrets_service.clear_secrets.return_value = ["telegram:Token"]  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "clear", TELEGRAM_SIDECAR_NAME])
    assert result.exit_code == 0
    services.secrets_service.clear_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]


def test_cleanup_removes_unknown_keys(runner: CliRunner) -> None:
    """'cleanup' delegates to secrets_service.cleanup_secrets."""
    services.secrets_service.cleanup_secrets.return_value = ["orphan:Key"]  # pyrefly: ignore [missing-attribute]

    result = runner.invoke(cli_root, ["secrets", "cleanup"])
    assert result.exit_code == 0
    services.secrets_service.cleanup_secrets.assert_called_once()  # pyrefly: ignore [missing-attribute]


@pytest.fixture
def sidecar_param(mocker: MockerFixture) -> click.Parameter:
    """Return the ``<sidecar>`` argument of ``secrets check``, sidecar list stubbed."""
    mocker.patch.object(
        services.secrets_service,
        "known_sidecars",
        return_value=["litellm-bedrock", TELEGRAM_SIDECAR_NAME],
    )
    check = secrets_group.commands["check"]
    return next(param for param in check.params if param.name == "sidecar")


@pytest.mark.parametrize(
    ("incomplete", "expected"),
    [
        ("", ["litellm-bedrock", TELEGRAM_SIDECAR_NAME]),
        ("lite", ["litellm-bedrock"]),
        ("tele", [TELEGRAM_SIDECAR_NAME]),
        ("zzz", []),
    ],
)
def test_sidecar_argument_completes_known_sidecars(
    sidecar_param: click.Parameter, incomplete: str, expected: list[str]
) -> None:
    """The one dynamic completion the wrapper has, now a click shell_complete callback."""
    ctx = click.Context(secrets_group.commands["check"], info_name="check")
    offered = [item.value for item in sidecar_param.shell_complete(ctx, incomplete)]
    assert offered == expected
