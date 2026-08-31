# This file has been edited with the assistance of an AI tool.
"""
CLI-layer tests for agent_wrap.cli.create — argument parsing and calling protocol.

``services.create_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from typing import TYPE_CHECKING

from agent_wrap.cli.create.complete import complete as create_complete
from agent_wrap.cli.create.run import run as create_run
from agent_wrap.containers import services

if TYPE_CHECKING:
    import pytest


def test_create_complete_no_completions() -> None:
    assert create_complete(2, ["agent", "create", ""]) == []


def test_create_delegates_to_service() -> None:
    """CLI entry point delegates to services.create_service.create()."""
    services.create_service.create.return_value = 0  # pyrefly: ignore [missing-attribute]
    rc = create_run([])
    assert rc == 0
    services.create_service.create.assert_called_once_with()  # pyrefly: ignore [missing-attribute]


def test_create_forwards_service_error_code() -> None:
    """Non-zero return from the service is forwarded to the caller."""
    services.create_service.create.return_value = 1  # pyrefly: ignore [missing-attribute]
    rc = create_run([])
    assert rc == 1
    services.create_service.create.assert_called_once_with()  # reason: service was called  # pyrefly: ignore [missing-attribute]


def test_create_rejects_extra_args(capsys: pytest.CaptureFixture[str]) -> None:
    """Create accepts no arguments (parser rejects extras)."""
    rc = create_run(["extra-arg"])
    assert rc != 0
    assert "unrecognized arguments" in capsys.readouterr().err
