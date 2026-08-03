# This file has been created with the assistance of an AI tool.
"""Completion for ``agent secrets`` — subcommands from parser choices, sidecar names via DI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.cli.secrets.constants import SUBCOMMAND_CWORD
from agent_wrap.cli.secrets.run import build_parser
from agent_wrap.containers import services

if TYPE_CHECKING:
    import argparse


def _subcommands(parser: argparse.ArgumentParser) -> frozenset[str]:
    for action in parser._actions:  # noqa: SLF001
        if action.dest == "action" and action.choices is not None:
            return frozenset(action.choices)
    return frozenset()


def complete(cword: int, words: list[str]) -> list[str]:
    parser = build_parser()
    if cword == SUBCOMMAND_CWORD:
        return sorted(_subcommands(parser))
    if cword > SUBCOMMAND_CWORD:
        sub = words[SUBCOMMAND_CWORD] if len(words) > SUBCOMMAND_CWORD else ""
        if sub in _subcommands(parser) - {"cleanup"}:
            return services.secrets_service.known_sidecars()
    return []
