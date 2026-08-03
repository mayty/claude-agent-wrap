# This file has been edited with the assistance of an AI tool.
"""Tests for CLI dispatch — guards against help/dispatch drift."""

from __future__ import annotations

import sys
from importlib import import_module
from typing import TYPE_CHECKING

from agent_wrap.__main__ import _complete, main
from agent_wrap.cli.commands import COMMANDS, command_meta
from agent_wrap.containers import services

if TYPE_CHECKING:
    import pytest
    from pytest_mock import MockerFixture


def test_every_command_module_exposes_run_and_metadata(subtests: pytest.Subtests) -> None:
    meta = command_meta()
    assert meta, "expected at least one command to be registered"
    for c in meta.values():
        with subtests.test(msg=c.name):  # type: ignore[bad-context-manager]
            mod = import_module(f"agent_wrap.cli.{c.name}.run")
            assert callable(getattr(mod, "run", None)), f"{c.name} missing callable run()"
            assert isinstance(c.usage, str), f"{c.name} USAGE must be a string"
            assert isinstance(c.summary, str), f"{c.name} SUMMARY must be a string"
            assert c.summary, f"{c.name} SUMMARY must be non-empty"


def test_help_lists_every_discovered_command(
    mocker: MockerFixture, subtests: pytest.Subtests
) -> None:
    mocker.patch("sys.argv", ["agent_wrap"])
    rc = main()
    assert rc == 1
    err_call = services.display_service.error.call_args  # type: ignore[union-attr]
    assert err_call is not None
    err_text = err_call[0][0]
    for c in command_meta().values():
        with subtests.test(msg=c.name):  # type: ignore[bad-context-manager]
            assert c.name in err_text, f"help output missing command {c.name!r}"
            if c.summary:
                assert c.summary in err_text, f"help output missing summary for {c.name!r}"


def test_unknown_command_returns_error(
    mocker: MockerFixture,
) -> None:
    mocker.patch("sys.argv", ["agent_wrap", "no-such-cmd"])
    rc = main()
    assert rc == 1
    services.display_service.error.assert_called_once_with(  # type: ignore[union-attr]
        "Unknown command: no-such-cmd"
    )


def test_complete_verb_completion(
    capsys: pytest.CaptureFixture[str], subtests: pytest.Subtests
) -> None:
    """Verify cword=1 prints all COMMANDS keys (verb completion)."""
    old_argv = sys.argv
    try:
        sys.argv = ["agent_wrap", "1", "agent", ""]
        _complete()
    finally:
        sys.argv = old_argv

    out = capsys.readouterr().out
    for name in COMMANDS:
        with subtests.test(msg=name):  # type: ignore[bad-context-manager]
            assert name in out


def test_complete_unknown_verb_no_output(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify cword > 1 with unknown verb produces no output."""
    old_argv = sys.argv
    try:
        sys.argv = ["agent_wrap", "2", "agent", "no-such-verb", ""]
        _complete()
    finally:
        sys.argv = old_argv

    assert capsys.readouterr().out == ""


def test_complete_known_verb_delegates_to_complete(capsys: pytest.CaptureFixture[str]) -> None:
    """Verify cword=2 with 'rebuild' delegates to rebuild's complete()."""
    old_argv = sys.argv
    try:
        sys.argv = ["agent_wrap", "2", "agent", "rebuild", ""]
        _complete()
    finally:
        sys.argv = old_argv

    out = capsys.readouterr().out
    assert "--full" in out


def test_every_command_has_complete_function(subtests: pytest.Subtests) -> None:
    """Every registered verb maps to a callable complete()."""
    for name, (_run_fn, complete_fn) in COMMANDS.items():
        with subtests.test(msg=name):  # type: ignore[bad-context-manager]
            assert callable(complete_fn), f"{name} complete() is not callable"


def test_commands_dict_matches_registered_verbs() -> None:
    """COMMANDS keys match the set of known verbs."""
    expected = {
        "cleanup",
        "create",
        "inspect",
        "logs",
        "rebuild",
        "run",
        "secrets",
        "stats",
        "update",
    }
    assert set(COMMANDS) == expected
