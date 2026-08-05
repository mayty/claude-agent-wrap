# This file has been created with the assistance of an AI tool.
"""Tests for the shared argparse helpers (parse_or_code / make_parser)."""

from __future__ import annotations

import argparse

import pytest

from agent_wrap.lib.argparsing import make_parser, parse_or_code, unused_flags


def _parser() -> argparse.ArgumentParser:
    parser = make_parser("demo", usage_summary="[--flag]")
    parser.add_argument("--flag", action="store_true")
    return parser


@pytest.fixture
def unused_flags_parser() -> argparse.ArgumentParser:
    """Build a test parser with known flags for unused_flags testing."""
    p = argparse.ArgumentParser()
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-f", "--from", dest="from_date")
    p.add_argument("-u", "--until", dest="until_date")
    p.add_argument("-d", "--days", type=int)
    p.add_argument("--hidden", help=argparse.SUPPRESS)
    p.add_argument("positional")
    return p


def test_unused_flags_all_flags_on_bare_tab(unused_flags_parser: argparse.ArgumentParser) -> None:
    """All non-hidden flags returned when nothing consumed."""
    result = unused_flags(unused_flags_parser, ["agent", "test", ""], 2)
    assert "-v" in result
    assert "--verbose" in result
    assert "-f" in result
    assert "--from" in result
    assert "-u" in result
    assert "--until" in result
    assert "-d" in result
    assert "--days" in result
    assert "-h" in result
    assert "--help" in result
    assert "--hidden" not in result


def test_unused_flags_consumed_flag_excluded(unused_flags_parser: argparse.ArgumentParser) -> None:
    """A used flag is removed from candidates."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "--verbose", ""], 3)
    assert "-v" not in result
    assert "--verbose" not in result
    assert "-f" in result  # still available


def test_unused_flags_shorthand_excludes_long_form(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """Using -v excludes --verbose (same action)."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "-v", ""], 3)
    assert "-v" not in result
    assert "--verbose" not in result


def test_unused_flags_long_form_excludes_shorthand(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """Using --from excludes -f (same action)."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "--from", "x", ""], 4)
    assert "-f" not in result
    assert "--from" not in result


def test_unused_flags_value_flag_prev_returns_empty(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """Previous word is a value-accepting flag → no flags returned."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "-d", ""], 3)
    assert result == []


def test_unused_flags_value_flag_with_value_then_tab_returns_unused(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """After the value is typed, unused flags are returned."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "-d", "14", ""], 4)
    assert "-d" not in result
    assert "--days" not in result
    assert "-v" in result


def test_unused_flags_equals_split_word_consumed(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """--from= (split by COMP_WORDBREAKS) still consumes --from/-f."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "--from=", "-14d", ""], 4)
    assert "-f" not in result
    assert "--from" not in result


def test_unused_flags_store_true_flag_not_value_accepting(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """Previous word is --verbose (store_true) → flags returned."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "--verbose", ""], 3)
    assert result != []


def test_unused_flags_prev_not_a_flag_returns_all_unused(
    unused_flags_parser: argparse.ArgumentParser,
) -> None:
    """Previous word is positional value → all unused flags returned."""
    result = unused_flags(unused_flags_parser, ["agent", "test", "foo", ""], 3)
    assert "-v" in result


def test_parse_or_code_returns_namespace_on_success() -> None:
    ns = parse_or_code(_parser(), ["--flag"])
    assert isinstance(ns, argparse.Namespace)
    assert ns.flag is True


def test_parse_or_code_returns_zero_on_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_or_code(_parser(), ["--help"]) == 0
    assert "demo" in capsys.readouterr().out


def test_parse_or_code_returns_one_on_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert parse_or_code(_parser(), ["--bogus"]) == 1
    assert capsys.readouterr().err


def test_make_parser_prog_and_abbrev_disabled() -> None:
    parser = _parser()
    assert parser.prog == "agent demo"
    # allow_abbrev=False: an unambiguous prefix must NOT match the long flag.
    assert parse_or_code(parser, ["--fl"]) == 1
