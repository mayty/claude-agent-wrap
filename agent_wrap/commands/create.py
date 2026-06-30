# This file has been edited with the assistance of an AI tool.
"""The `create` subcommand — scaffolds a Dockerfile.agent."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_wrap.lib.argparsing import make_parser, parse_or_code
from agent_wrap.lib.utils import sanitize_name

USAGE = ""
SUMMARY = "Scaffold Dockerfile.agent"


def run(args: list[str], tool_dir: Path) -> int:  # noqa: ARG001
    """Execute the `create` subcommand."""
    ns = parse_or_code(make_parser("create", usage_summary=USAGE), args)
    if isinstance(ns, int):
        return ns

    dst = Path.cwd() / "Dockerfile.agent"

    if dst.exists():
        print(f"Error: {dst} already exists", file=sys.stderr)
        return 1

    name = sanitize_name(Path.cwd().name)
    if not name:
        print(
            f"Error: could not derive agent-name from directory '{Path.cwd()}'",
            file=sys.stderr,
        )
        return 1

    dst.write_text(
        f"# agent-name: {name}\nFROM claude-agent\n\n# Add project-specific RUN steps here.\n"
    )
    print(f"Created {dst} with agent-name '{name}' (FROM claude-agent)")
    return 0
