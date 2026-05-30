#!/usr/bin/env python3
# This file has been created with the assistance of an AI tool.
"""agent-wrap CLI entry point.

Dispatches subcommands to the agent_wrap.commands package.
Called from agent-wrap.bashrc as: python3 main.py <subcommand> [args...]
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print(
            "Usage: main.py <command> [args...]\n\n"
            "Commands:\n"
            "  agent    [--base] [claude-args...]  Launch Claude Code in Docker\n"
            "  rebuild  [--full]                   Rebuild Docker image\n"
            "  create                              Scaffold Dockerfile.agent\n"
            "  usage    [--days N] [--region L]    Show token usage stats\n"
            "  update                              Pull upstream updates\n",
            file=sys.stderr,
        )
        return 1

    command = sys.argv[1]
    args = sys.argv[2:]
    tool_dir = Path(__file__).parent.resolve()

    if command == "agent":
        from agent_wrap.commands.agent import run
        return run(args, tool_dir)
    elif command == "rebuild":
        from agent_wrap.commands.rebuild import run
        return run(args, tool_dir)
    elif command == "create":
        from agent_wrap.commands.create import run
        return run(args, tool_dir)
    elif command == "usage":
        from agent_wrap.commands.usage import run
        return run(args, tool_dir)
    elif command == "update":
        from agent_wrap.commands.update import run
        return run(args, tool_dir)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
