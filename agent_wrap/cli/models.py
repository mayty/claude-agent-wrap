# This file has been created with the assistance of an AI tool.
"""Data/type-carrying classes for the CLI layer."""

from collections.abc import Callable
from dataclasses import dataclass

#: A subcommand's entry point: argv tail in, exit code out.
type RunFunc = Callable[[list[str]], int]
#: A subcommand's completion hook: (cword, words) in, candidates out.
type CompleteFunc = Callable[[int, list[str]], list[str]]


@dataclass(frozen=True)
class Command:
    """A registered subcommand's help metadata, read reflectively off its module."""

    name: str
    usage: str
    summary: str
