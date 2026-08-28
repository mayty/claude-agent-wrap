# This file has been edited with the assistance of an AI tool.
"""
agent-wrap CLI entry point.

Normal path:  python3 -m agent_wrap <verb> [args...]
Complete path: AGENT_COMPLETE=1 python3 -m agent_wrap <cword> <word0> ...
"""

from __future__ import annotations

import os
import sys

from agent_wrap.cli.commands import command_meta, format_usage
from agent_wrap.cli.constants import COMMANDS
from agent_wrap.constants import MIN_ARGS
from agent_wrap.containers import services


def main() -> int:
    """Run the normal CLI dispatch path."""
    if len(sys.argv) < MIN_ARGS:
        meta = command_meta()
        services.display_service.info(format_usage(meta), end="")
        return 1

    name = sys.argv[1]
    args = sys.argv[2:]

    entry = COMMANDS.get(name)
    if entry is None:
        services.display_service.error(f"Unknown command: {name}")
        return 1

    run_fn, _complete_fn = entry
    return run_fn(args)


def _complete() -> None:
    """Run the tab-completion dispatch path.  Prints candidates to stdout."""
    cword = int(sys.argv[1])
    words = sys.argv[2:]
    verb = words[1] if len(words) > 1 else ""

    if cword <= 1:
        # Completing the verb itself
        for name in sorted(COMMANDS):
            print(name)
    elif verb in COMMANDS:
        _run_fn, complete_fn = COMMANDS[verb]
        for candidate in complete_fn(cword, words):
            print(candidate)


if __name__ == "__main__":
    if os.environ.get("AGENT_COMPLETE"):
        _complete()
    else:
        sys.exit(main())
