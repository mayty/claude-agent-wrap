# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.secrets — argument parsing and calling protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.cli.commands import COMMANDS
from agent_wrap.cli.secrets.run import run as secrets_run
from agent_wrap.constants import TELEGRAM_SIDECAR_NAME
from agent_wrap.containers import services

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
    services.display_service.error.assert_called_once_with(  # type: ignore[union-attr]
        "The 'check' action requires a sidecar name.  Usage: agent secrets check <sidecar>"
    )


def test_run_cleanup_rejects_extra_arg() -> None:
    """'cleanup' with an extra argument returns 1."""
    assert secrets_run(["cleanup", "extra"]) == 1
    services.display_service.error.assert_called_once_with(  # type: ignore[union-attr]
        "The 'cleanup' action does not take a sidecar argument."
    )


def test_run_set_requires_sidecar() -> None:
    """'set' without a sidecar name returns 1."""
    assert secrets_run(["set"]) == 1
    services.display_service.error.assert_called_once_with(  # type: ignore[union-attr]
        "The 'set' action requires a sidecar name.  Usage: agent secrets set <sidecar>"
    )


def test_run_clear_requires_sidecar() -> None:
    """'clear' without a sidecar name returns 1."""
    assert secrets_run(["clear"]) == 1
    services.display_service.error.assert_called_once_with(  # type: ignore[union-attr]
        "The 'clear' action requires a sidecar name.  Usage: agent secrets clear <sidecar>"
    )


def test_run_check_calls_sidecar_secrets_service() -> None:
    """'check telegram' calls secrets_service.check_secrets."""
    services.secrets_service.get_required_secrets.return_value = [  # type: ignore[union-attr]
        ("TelegramBotToken", "desc"),
        ("TelegramChatId", "desc"),
    ]
    services.secrets_service.check_secrets.return_value = {  # type: ignore[union-attr]
        "telegram:TelegramBotToken": True,
        "telegram:TelegramChatId": True,
    }

    rc = secrets_run(["check", TELEGRAM_SIDECAR_NAME])
    assert rc == 0
    services.secrets_service.check_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # type: ignore[union-attr]


def test_run_check_missing_secret_returns_one() -> None:
    """'check' returns 1 when a required secret is missing."""
    services.secrets_service.get_required_secrets.return_value = [("Token", "desc")]  # type: ignore[union-attr]
    services.secrets_service.check_secrets.return_value = {"telegram:Token": False}  # type: ignore[union-attr]

    rc = secrets_run(["check", TELEGRAM_SIDECAR_NAME])
    assert rc == 1
    services.secrets_service.check_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # type: ignore[union-attr]


def test_run_set_non_interactive_returns_one() -> None:
    """'set' returns 1 when set_secrets raises RuntimeError."""
    services.secrets_service.set_secrets.side_effect = RuntimeError("non-interactive")  # type: ignore[union-attr]

    rc = secrets_run(["set", TELEGRAM_SIDECAR_NAME])
    assert rc == 1
    services.secrets_service.set_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # type: ignore[union-attr]


def test_run_clear_removes_namespaced_keys() -> None:
    """'clear telegram' delegates to secrets_service.clear_secrets."""
    services.secrets_service.clear_secrets.return_value = ["telegram:Token"]  # type: ignore[union-attr]

    rc = secrets_run(["clear", TELEGRAM_SIDECAR_NAME])
    assert rc == 0
    services.secrets_service.clear_secrets.assert_called_once_with(TELEGRAM_SIDECAR_NAME)  # type: ignore[union-attr]


def test_run_cleanup_removes_unknown_keys() -> None:
    """'cleanup' delegates to secrets_service.cleanup_secrets."""
    services.secrets_service.cleanup_secrets.return_value = ["orphan:Key"]  # type: ignore[union-attr]

    rc = secrets_run(["cleanup"])
    assert rc == 0
    services.secrets_service.cleanup_secrets.assert_called_once()  # type: ignore[union-attr]


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
