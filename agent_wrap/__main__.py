#!/usr/bin/env python3
# This file has been created with the assistance of an AI tool.
"""
agent-wrap CLI entry point.

Dispatches subcommands to the agent_wrap.commands package.
Called from agent-wrap.bashrc as: python3 -m agent_wrap <subcommand> [args...]
"""

from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

_COMMANDS = {
    "agent": "agent_wrap.commands.agent",
    "rebuild": "agent_wrap.commands.rebuild",
    "create": "agent_wrap.commands.create",
    "usage": "agent_wrap.commands.usage",
    "update": "agent_wrap.commands.update",
}

_MIN_ARGS = 2

_USAGE = """\
Usage: python3 -m agent_wrap <command> [args...]

Commands:
  agent    [--base] [claude-args...]  Launch Claude Code in Docker
  rebuild  [--full]                   Rebuild Docker image
  create                              Scaffold Dockerfile.agent
  usage    [--days N] [--region L]    Show token usage stats
  update                              Pull upstream updates
"""


def main() -> int:
    if len(sys.argv) < _MIN_ARGS:
        print(_USAGE, file=sys.stderr)
        return 1

    command = sys.argv[1]
    args = sys.argv[2:]
    tool_dir = Path(__file__).parent.parent.resolve()

    module_path = _COMMANDS.get(command)
    if module_path is None:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1

    mod = import_module(module_path)
    run: Callable[..., int] = mod.run
    return run(args, tool_dir)


if __name__ == "__main__":
    sys.exit(main())
