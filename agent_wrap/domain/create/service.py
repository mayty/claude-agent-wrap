# This file has been created with the assistance of an AI tool.
"""Dockerfile.agent scaffolding domain service."""

from __future__ import annotations

import sys
from pathlib import Path

from agent_wrap.lib.utils import sanitize_name


class CreateService:
    """Scaffolds a Dockerfile.agent with agent-name and FROM directives."""

    def create(self) -> int:
        """Scaffold a Dockerfile.agent in the current directory. Returns exit code."""
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
