# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.secrets — argument parsing and calling protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.cli.constants import COMMANDS
from agent_wrap.cli.secrets.run import run as secrets_run
from agent_wrap.constants import TELEGRAM_SIDECAR_NAME
from agent_wrap.containers import services
from agent_wrap.domain.secrets.models import SecretsCheckReport, SecretsSetResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from pytest_mock import MockerFixture


def test_run_requires_action(capsys: pytest.CaptureFixture[str]) -> None:
    """Running with no action returns 1."""
    assert secrets_run([]) == 1
    assert "usage:" in capsys.readouterr().err  # argparse standard format


def test_run_check_requires_sidecar() -> None:
    """'check' without a sidecar name returns 1."""
    assert secrets_run(["check"]) == 1
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "The 'check' action requires a sidecar name.  Usage: agent secrets check <sidecar>"
    )


def test_run_cleanup_rejects_extra_arg() -> None:
    """'cleanup' with an extra argument returns 1."""
    assert secrets_run(["cleanup", "extra"]) == 1
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "The 'cleanup' action does not take a sidecar argument."
    )


def test_run_set_requires_sidecar() -> None:
    """'set' without a sidecar name returns 1."""
    assert secrets_run(["set"]) == 1
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "The 'set' action requires a sidecar name.  Usage: agent secrets set <sidecar>"
    )


def test_run_clear_requires_sidecar() -> None:
    """'clear' without a sidecar name returns 1."""
    assert secrets_run(["clear"]) == 1
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "The 'clear' action requires a sidecar name.  Usage: agent secrets clear <sidecar>"
    )


def test_run_check_reports_all_present() -> None:
    """'check telegram' returns 0 when the report says every secret is present."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:TelegramBotToken": True, "telegram:TelegramChatId": True},
        all_present=True,
        declares_none=False,
    )

    assert secrets_run(["check", TELEGRAM_SIDECAR_NAME]) == 0
    services.secrets_service.check_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]


def test_run_check_missing_secret_returns_one() -> None:
    """'check' returns 1 when the report's verdict is not all-present."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={"telegram:Token": False}, all_present=False, declares_none=False
    )

    assert secrets_run(["check", TELEGRAM_SIDECAR_NAME]) == 1
    services.display_service.error.assert_called_once()  # pyrefly: ignore [missing-attribute]


def test_run_check_declares_no_secrets_returns_zero() -> None:
    """A sidecar requiring nothing is reported as such, not as an empty pass."""
    services.secrets_service.check_secrets.return_value = SecretsCheckReport(  # pyrefly: ignore [missing-attribute]
        entries={}, all_present=True, declares_none=True
    )

    assert secrets_run(["check", TELEGRAM_SIDECAR_NAME]) == 0
    assert "declares no secrets" in services.display_service.info.call_args[0][0]  # pyrefly: ignore [missing-attribute]


def test_run_set_non_interactive_returns_one() -> None:
    """'set' reports the result's error and fails when there is no TTY."""
    services.secrets_service.set_secrets.return_value = SecretsSetResult(  # pyrefly: ignore [missing-attribute]
        keys_set=[], error="Cannot prompt for secrets in a non-interactive session."
    )

    assert secrets_run(["set", TELEGRAM_SIDECAR_NAME]) == 1
    services.secrets_service.set_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]
    services.display_service.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        "Cannot prompt for secrets in a non-interactive session."
    )


def test_run_set_succeeds_when_keys_were_set() -> None:
    services.secrets_service.set_secrets.return_value = SecretsSetResult(  # pyrefly: ignore [missing-attribute]
        keys_set=["telegram:Token"]
    )

    assert secrets_run(["set", TELEGRAM_SIDECAR_NAME]) == 0
    services.display_service.error.assert_not_called()  # pyrefly: ignore [missing-attribute]


def test_run_clear_removes_namespaced_keys() -> None:
    """'clear telegram' delegates to secrets_service.clear_secrets."""
    services.secrets_service.clear_secrets.return_value = ["telegram:Token"]  # pyrefly: ignore [missing-attribute]

    rc = secrets_run(["clear", TELEGRAM_SIDECAR_NAME])
    assert rc == 0
    services.secrets_service.clear_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # pyrefly: ignore [missing-attribute]


def test_run_cleanup_removes_unknown_keys() -> None:
    """'cleanup' delegates to secrets_service.cleanup_secrets."""
    services.secrets_service.cleanup_secrets.return_value = ["orphan:Key"]  # pyrefly: ignore [missing-attribute]

    rc = secrets_run(["cleanup"])
    assert rc == 0
    services.secrets_service.cleanup_secrets.assert_called_once()  # pyrefly: ignore [missing-attribute]


@pytest.fixture
def run_complete() -> Callable[[int, list[str]], list[str]]:
    """Return a callable that invokes the registered complete() for 'secrets'."""
    _run_fn, complete_fn = COMMANDS["secrets"]
    return complete_fn


def test_complete_cword_2_shows_subcommands(
    run_complete: Callable[[int, list[str]], list[str]],
) -> None:
    result = run_complete(2, ["agent", "secrets", ""])
    assert "check" in result
    assert "set" in result
    assert "clear" in result
    assert "cleanup" in result


def test_complete_cword_3_cleanup_no_sidecar(
    run_complete: Callable[[int, list[str]], list[str]],
) -> None:
    result = run_complete(3, ["agent", "secrets", "cleanup", ""])
    assert result == []


def test_complete_cword_3_check_shows_sidecars(
    run_complete: Callable[[int, list[str]], list[str]],
    mocker: MockerFixture,
) -> None:
    """Verify sidecar names include 'telegram' (always present)."""
    mocker.patch.object(
        services.secrets_service,
        "known_sidecars",
        return_value=["litellm-bedrock", TELEGRAM_SIDECAR_NAME],
    )
    result = run_complete(3, ["agent", "secrets", "check", ""])
    assert TELEGRAM_SIDECAR_NAME in result
    assert "litellm-bedrock" in result
    assert len(result) == 2
