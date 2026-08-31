# This file has been created with the assistance of an AI tool.
"""Completion for ``agent rebuild`` — flags from the parser."""

from agent_wrap.cli.rebuild.run import build_parser
from agent_wrap.lib.argparsing import unused_flags


def complete(cword: int, words: list[str]) -> list[str]:
    return unused_flags(build_parser(), words, cword)
