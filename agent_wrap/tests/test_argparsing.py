# This file has been created with the assistance of an AI tool.
"""Tests for the shared argparse helpers (parse_or_code / make_parser)."""

from __future__ import annotations

import argparse

import pytest

from agent_wrap.lib.argparsing import make_parser, parse_or_code


def _parser() -> argparse.ArgumentParser:
    parser = make_parser("demo", usage_summary="[--flag]")
    parser.add_argument("--flag", action="store_true")
    return parser


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
