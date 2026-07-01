# This file has been created with the assistance of an AI tool.
"""
CLI dispatch for agent-wrap.

Resolves ``sys.argv[1]`` to a registered subcommand and delegates to its
``run()`` entry point. Help text is generated from each command's USAGE and
SUMMARY module-level strings.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import import_module

from agent_wrap.cli.commands import COMMANDS

_MIN_ARGS = 2


@dataclass(frozen=True)
class _Command:
    name: str
    usage: str
    summary: str


def command_meta() -> dict[str, _Command]:
    """Return metadata for every registered command keyed by name."""
    meta: dict[str, _Command] = {}
    for name in COMMANDS:
        mod = import_module(f"agent_wrap.cli.{name}")
        meta[name] = _Command(
            name=name,
            usage=getattr(mod, "USAGE", ""),
            summary=getattr(mod, "SUMMARY", ""),
        )
    return meta


def _format_usage(commands: dict[str, _Command]) -> str:
    """Render the help block from registered commands."""
    name_width = max((len(c.name) for c in commands.values()), default=0)
    usage_width = max((len(c.usage) for c in commands.values()), default=0)
    rows = [
        f"  {c.name:<{name_width}}  {c.usage:<{usage_width}}  {c.summary}".rstrip()
        for c in commands.values()
    ]
    return "\n".join(["Usage: agent <command> [args...]", "", "Commands:", *rows]) + "\n"


def main() -> int:
    if len(sys.argv) < _MIN_ARGS:
        meta = command_meta()
        print(_format_usage(meta), file=sys.stderr, end="")
        return 1

    name = sys.argv[1]
    args = sys.argv[2:]

    run = COMMANDS.get(name)
    if run is None:
        print(f"Unknown command: {name}", file=sys.stderr)
        return 1

    return run(args)
