# This file has been edited with the assistance of an AI tool.
"""
agent-wrap CLI entry point.

Dispatches subcommands to the agent_wrap.commands package.
Called from agent-wrap.bashrc as: python3 -m agent_wrap <subcommand> [args...]
"""

from __future__ import annotations

import pkgutil
import sys
from dataclasses import dataclass
from importlib import import_module
from typing import TYPE_CHECKING

from agent_wrap import commands as commands_pkg

if TYPE_CHECKING:
    from collections.abc import Callable

_MIN_ARGS = 2


@dataclass(frozen=True)
class _Command:
    name: str
    module_path: str
    usage: str
    summary: str


def _discover_commands() -> list[_Command]:
    """Discover subcommand modules under agent_wrap.commands, sorted alphabetically."""

    def _build(info: pkgutil.ModuleInfo) -> _Command:
        module_path = f"{commands_pkg.__name__}.{info.name}"
        mod = import_module(module_path)
        return _Command(
            name=info.name,
            module_path=module_path,
            usage=getattr(mod, "USAGE", ""),
            summary=getattr(mod, "SUMMARY", ""),
        )

    discovered = [
        _build(info)
        for info in pkgutil.iter_modules(commands_pkg.__path__)
        if not info.ispkg and not info.name.startswith("_")
    ]
    discovered.sort(key=lambda c: c.name)
    return discovered


def _format_usage(commands: list[_Command]) -> str:
    """Render the help block from discovered commands."""
    name_width = max((len(c.name) for c in commands), default=0)
    usage_width = max((len(c.usage) for c in commands), default=0)
    rows = [
        f"  {c.name:<{name_width}}  {c.usage:<{usage_width}}  {c.summary}".rstrip()
        for c in commands
    ]
    return "\n".join(["Usage: agent <command> [args...]", "", "Commands:", *rows]) + "\n"


def main() -> int:
    commands = _discover_commands()

    if len(sys.argv) < _MIN_ARGS:
        print(_format_usage(commands), file=sys.stderr, end="")
        return 1

    name = sys.argv[1]
    args = sys.argv[2:]

    match = next((c for c in commands if c.name == name), None)
    if match is None:
        print(f"Unknown command: {name}", file=sys.stderr)
        return 1

    mod = import_module(match.module_path)
    run: Callable[..., int] = mod.run
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
