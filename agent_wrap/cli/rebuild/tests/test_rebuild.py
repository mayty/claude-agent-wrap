# This file has been edited with the assistance of an AI tool.
"""CLI-layer tests for agent_wrap.cli.rebuild — argument parsing and calling protocol."""

from __future__ import annotations

import pytest

from agent_wrap.cli.rebuild.complete import complete as rebuild_complete
from agent_wrap.cli.rebuild.run import build_parser
from agent_wrap.cli.rebuild.run import run as rebuild_run


def test_parse_no_args() -> None:
    assert build_parser().parse_args([]).full is False


def test_parse_full_flag() -> None:
    assert build_parser().parse_args(["--full"]).full is True


def test_parse_f_flag() -> None:
    """-f is the shorthand for --full."""
    assert build_parser().parse_args(["-f"]).full is True


def test_help_short() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["-h"])
    assert exc.value.code == 0


def test_help_long() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--help"])
    assert exc.value.code == 0


def test_unknown_arg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--bogus"])
    assert exc.value.code != 0
    assert "unrecognized arguments" in capsys.readouterr().err


def test_run_help_returns_zero() -> None:
    assert rebuild_run(["-h"]) == 0


def test_run_unknown_arg_returns_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert rebuild_run(["--bogus"]) == 1
    assert "unrecognized arguments" in capsys.readouterr().err


def test_complete_bare_tab_shows_flags() -> None:
    result = rebuild_complete(2, ["agent", "rebuild", ""])
    assert "--full" in result
    assert "-f" in result
    assert "-h" in result


def test_complete_flag_partial() -> None:
    result = rebuild_complete(2, ["agent", "rebuild", "--f"])
    assert "--full" in result
    # compgen filters, but we return all candidates


def test_complete_flag_consumed() -> None:
    result = rebuild_complete(3, ["agent", "rebuild", "--full", ""])
    assert "--full" not in result
