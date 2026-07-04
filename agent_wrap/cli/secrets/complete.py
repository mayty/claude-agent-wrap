# This file has been created with the assistance of an AI tool.
"""Completion for ``agent secrets`` — subcommands from parser choices, sidecar names via DI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.cli.secrets.run import build_parser
from agent_wrap.containers import services

if TYPE_CHECKING:
    import argparse


def _subcommands(parser: argparse.ArgumentParser) -> frozenset[str]:
    for action in parser._actions:  # noqa: SLF001
        if action.dest == "action" and action.choices is not None:
            return frozenset(action.choices)
    return frozenset()


# The word index right after the verb (COMP_WORDS positions are 0-based).
_SUBCOMMAND_CWORD = 2
_SUBCOMMAND_INDEX = 2


def complete(cword: int, words: list[str]) -> list[str]:
    parser = build_parser()
    if cword == _SUBCOMMAND_CWORD:
        return sorted(_subcommands(parser))
    if cword > _SUBCOMMAND_CWORD:
        sub = words[_SUBCOMMAND_INDEX] if len(words) > _SUBCOMMAND_INDEX else ""
        if sub in _subcommands(parser) - {"cleanup"}:
            return services.secrets_service.known_sidecars()
    return []
