# This file has been created with the assistance of an AI tool.
"""Data models for the build domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class DockerfileAgentInfo:
    """Parsed directives from a Dockerfile.agent file."""

    agent_user: str = "ubuntu"
    expose_ports: list[str] = field(default_factory=list)
    extra_run_args: list[str] = field(default_factory=list)


@dataclass
class ResolvedImage:
    """Result of resolving which Docker image to use."""

    image: str
    dockerfile: Path
    context: Path
