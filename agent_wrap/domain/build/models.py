# This file has been edited with the assistance of an AI tool.
"""Data models for the build domain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path


class DockerfileLocation(NamedTuple):
    """Where a project's Dockerfile was found, if anywhere."""

    #: The resolved file, or None when the project declares no Dockerfile.
    path: Path | None
    #: True when it was found at the deprecated ``<project>/Dockerfile.agent`` path.
    is_legacy: bool


@dataclass
class DockerfileAgentInfo:
    """Parsed directives from a project Dockerfile."""

    agent_user: str = "ubuntu"
    expose_ports: list[str] = field(default_factory=list)
    extra_run_args: list[str] = field(default_factory=list)
    #: Timeout for the project startup script, or None when startup is not enabled.
    startup_timeout: float | None = None


@dataclass
class ResolvedImage:
    """Result of resolving which Docker image to use."""

    image: str
    dockerfile: Path
    context: Path
    #: The ``# agent-name:`` this image was tagged from, or None for the base image.
    #: ``agent_name is not None`` is the "this is a project Dockerfile" predicate --
    #: the file's basename is not, since the project file and ``ops/Dockerfile`` share it.
    agent_name: str | None = None
    #: True when the project Dockerfile came from the deprecated location.
    is_legacy: bool = False
