# This file has been edited with the assistance of an AI tool.
"""Project Dockerfile scaffolding domain service."""

from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    BASE_IMAGE_NAME,
    LEGACY_AGENT_DOCKERFILE_NAME,
)
from agent_wrap.lib.utils import sanitize_name

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


class CreateService:
    """Scaffolds a project Dockerfile with agent-name and FROM directives."""

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    def create(self) -> int:
        """Scaffold a project Dockerfile in the current directory. Returns exit code."""
        cwd = Path.cwd()
        dst = cwd / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME

        if dst.exists():
            self._display.error(f"{dst} already exists")
            return 1

        legacy = cwd / LEGACY_AGENT_DOCKERFILE_NAME
        if legacy.exists():
            self._display.error(
                f"{legacy} already exists. Move it to "
                f"{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME} instead of scaffolding a new one."
            )
            return 1

        name = sanitize_name(cwd.name)
        if not name:
            self._display.error(f"could not derive agent-name from directory '{cwd}'")
            return 1

        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            f"# agent-name: {name}\nFROM {BASE_IMAGE_NAME}\n\n"
            f"# Add project-specific RUN steps here.\n"
        )
        self._display.success(f"Created {dst} with agent-name '{name}' (FROM {BASE_IMAGE_NAME})")
        return 0
