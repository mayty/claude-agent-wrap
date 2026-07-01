# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/__main__.py — guards against help/dispatch drift."""

from __future__ import annotations

import pytest

from agent_wrap.__main__ import _discover_commands, main


def test_every_command_module_exposes_run_and_metadata() -> None:
    commands = _discover_commands()
    assert commands, "expected at least one command to be discovered"
    for c in commands:
        from importlib import import_module

        mod = import_module(c.module_path)
        assert callable(getattr(mod, "run", None)), f"{c.name} missing callable run()"
        assert isinstance(c.usage, str), f"{c.name} USAGE must be a string"
        assert isinstance(c.summary, str), f"{c.name} SUMMARY must be a string"
        assert c.summary, f"{c.name} SUMMARY must be non-empty"


def test_help_lists_every_discovered_command(mocker, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("sys.argv", ["agent_wrap"])
    rc = main()
    assert rc == 1
    err = capsys.readouterr().err
    for c in _discover_commands():
        assert c.name in err, f"help output missing command {c.name!r}"
        if c.summary:
            assert c.summary in err, f"help output missing summary for {c.name!r}"


def test_unknown_command_returns_error(mocker, capsys: pytest.CaptureFixture[str]) -> None:
    mocker.patch("sys.argv", ["agent_wrap", "no-such-cmd"])
    rc = main()
    assert rc == 1
    assert "Unknown command: no-such-cmd" in capsys.readouterr().err
