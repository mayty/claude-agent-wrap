# This file has been edited with the assistance of an AI tool.
"""Tests for CLI dispatch — guards against help/dispatch drift."""

from __future__ import annotations

from importlib import import_module

import pytest
from pytest_mock import MockerFixture
from pytest_subtests import SubTests

from agent_wrap.__main__ import main
from agent_wrap.cli.commands import command_meta


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


def test_help_lists_every_discovered_command(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str], subtests: SubTests
) -> None:
    mocker.patch("sys.argv", ["agent_wrap"])
    rc = main()
    assert rc == 1
    err = capsys.readouterr().err
    for c in command_meta().values():
        with subtests.test(msg=c.name):  # type: ignore[bad-context-manager]
            assert c.name in err, f"help output missing command {c.name!r}"
            if c.summary:
                assert c.summary in err, f"help output missing summary for {c.name!r}"


def test_unknown_command_returns_error(
    mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    mocker.patch("sys.argv", ["agent_wrap", "no-such-cmd"])
    rc = main()
    assert rc == 1
    assert "Unknown command: no-such-cmd" in capsys.readouterr().err
