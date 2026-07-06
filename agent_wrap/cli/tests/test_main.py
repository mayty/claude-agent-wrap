# This file has been edited with the assistance of an AI tool.
"""Tests for CLI dispatch — guards against help/dispatch drift."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from agent_wrap.__main__ import main
from agent_wrap.cli.commands import command_meta
from agent_wrap.containers import services

if TYPE_CHECKING:
    from pytest_mock import MockerFixture
    from pytest_subtests import SubTests


def test_every_command_module_exposes_run_and_metadata(subtests: SubTests) -> None:
    meta = command_meta()
    assert meta, "expected at least one command to be registered"
    for c in meta.values():
        with subtests.test(msg=c.name):  # type: ignore[bad-context-manager]
            mod = import_module(f"agent_wrap.cli.{c.name}.run")
            assert callable(getattr(mod, "run", None)), f"{c.name} missing callable run()"
            assert isinstance(c.usage, str), f"{c.name} USAGE must be a string"
            assert isinstance(c.summary, str), f"{c.name} SUMMARY must be a string"
            assert c.summary, f"{c.name} SUMMARY must be non-empty"


def test_help_lists_every_discovered_command(mocker: MockerFixture, subtests: SubTests) -> None:
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
