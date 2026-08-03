# This file has been created with the assistance of an AI tool.
"""Tab completion for the `inspect` subcommand."""

from __future__ import annotations

from agent_wrap.cli.inspect.run import build_parser
from agent_wrap.lib.argparsing import unused_flags


def complete(cword: int, words: list[str]) -> list[str]:
    return unused_flags(build_parser(), words, cword)
