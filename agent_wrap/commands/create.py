# This file has been created with the assistance of an AI tool.
"""The `create` subcommand — scaffolds a Dockerfile.agent."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_wrap.utils import sanitize_name


def run(args: list[str], tool_dir: Path) -> int:  # noqa: ARG001
    """Execute the `create` subcommand."""
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
        f"# agent-name: {name}\n"
        f"# This file has been created with the assistance of an AI tool.\n"
        f"FROM claude-agent\n"
        f"\n"
        f"# Add project-specific RUN steps here.\n"
    )
    print(f"Created {dst} with agent-name '{name}' (FROM claude-agent)")
    return 0
