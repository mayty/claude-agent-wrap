# This file has been edited with the assistance of an AI tool.
"""Dockerfile.agent scaffolding domain service."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.lib.utils import sanitize_name

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


class CreateService:
    """Scaffolds a Dockerfile.agent with agent-name and FROM directives."""

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    def create(self) -> int:
        """Scaffold a Dockerfile.agent in the current directory. Returns exit code."""
        dst = Path.cwd() / "Dockerfile.agent"

        if dst.exists():
            self._display.error(f"Error: {dst} already exists")
            return 1

        name = sanitize_name(Path.cwd().name)
        if not name:
            self._display.error(f"Error: could not derive agent-name from directory '{Path.cwd()}'")
            return 1

        dst.write_text(
            f"# agent-name: {name}\nFROM claude-agent\n\n# Add project-specific RUN steps here.\n"
        )
        self._display.success(f"Created {dst} with agent-name '{name}' (FROM claude-agent)")
        return 0
