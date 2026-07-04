# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.secrets — argument parsing and calling protocol."""

from __future__ import annotations

import pytest

from agent_wrap.cli.secrets.run import run as secrets_run
from agent_wrap.containers import services


def test_run_requires_action(capsys: pytest.CaptureFixture[str]) -> None:
    """Running with no action returns 1."""
    assert secrets_run([]) == 1
    assert "usage:" in capsys.readouterr().err  # argparse standard format


def test_run_check_requires_sidecar(capsys: pytest.CaptureFixture[str]) -> None:
    """'check' without a sidecar name returns 1."""
    assert secrets_run(["check"]) == 1
    assert "Usage:" in capsys.readouterr().err


def test_run_cleanup_rejects_extra_arg(capsys: pytest.CaptureFixture[str]) -> None:
    """'cleanup' with an extra argument returns 1."""
    assert secrets_run(["cleanup", "extra"]) == 1
    assert "does not take a sidecar argument" in capsys.readouterr().err


def test_run_set_requires_sidecar(capsys: pytest.CaptureFixture[str]) -> None:
    """'set' without a sidecar name returns 1."""
    assert secrets_run(["set"]) == 1
    assert "Usage:" in capsys.readouterr().err


def test_run_clear_requires_sidecar(capsys: pytest.CaptureFixture[str]) -> None:
    """'clear' without a sidecar name returns 1."""
    assert secrets_run(["clear"]) == 1
    assert "Usage:" in capsys.readouterr().err


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

    rc = secrets_run(["check", "telegram"])
    assert rc == 0
    services.secrets_service.check_secrets.assert_called_once_with("telegram")  # type: ignore[union-attr]


def test_run_check_missing_secret_returns_one() -> None:
    """'check' returns 1 when a required secret is missing."""
    services.secrets_service.get_required_secrets.return_value = [("Token", "desc")]  # type: ignore[union-attr]
    services.secrets_service.check_secrets.return_value = {"telegram:Token": False}  # type: ignore[union-attr]

    rc = secrets_run(["check", "telegram"])
    assert rc == 1
    services.secrets_service.check_secrets.assert_called_once_with("telegram")  # type: ignore[union-attr]


def test_run_set_non_interactive_returns_one() -> None:
    """'set' returns 1 when set_secrets raises RuntimeError."""
    services.secrets_service.set_secrets.side_effect = RuntimeError("non-interactive")  # type: ignore[union-attr]

    rc = secrets_run(["set", "telegram"])
    assert rc == 1
    services.secrets_service.set_secrets.assert_called_once_with("telegram")  # type: ignore[union-attr]


def test_run_clear_removes_namespaced_keys() -> None:
    """'clear telegram' delegates to secrets_service.clear_secrets."""
    services.secrets_service.clear_secrets.return_value = ["telegram:Token"]  # type: ignore[union-attr]

    rc = secrets_run(["clear", "telegram"])
    assert rc == 0
    services.secrets_service.clear_secrets.assert_called_once_with("telegram")  # type: ignore[union-attr]


def test_run_cleanup_removes_unknown_keys() -> None:
    """'cleanup' delegates to secrets_service.cleanup_secrets."""
    services.secrets_service.cleanup_secrets.return_value = ["orphan:Key"]  # type: ignore[union-attr]

    rc = secrets_run(["cleanup"])
    assert rc == 0
    services.secrets_service.cleanup_secrets.assert_called_once()  # type: ignore[union-attr]
