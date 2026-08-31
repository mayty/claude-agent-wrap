# This file has been edited with the assistance of an AI tool.
"""Data models for the build domain."""

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


class ImageStaleness(NamedTuple):
    """
    Why each image would be rebuilt if asked right now, for read-only reporting.

    Each field is the rendered reason line, or "" when that image is current, absent from
    the question (no project Dockerfile), or could not be checked at all.
    """

    base: str
    project: str


class StaleProjectImage(NamedTuple):
    """
    One registered project whose per-project image would be rebuilt on its next launch.

    A row of the fleet-wide sweep :meth:`BuildService.stale_project_images` performs, as
    opposed to :class:`ImageStaleness`, which answers the same question for the cwd alone.
    Projects that declare no Dockerfile never appear: their target is the base image, whose
    staleness is one fact about this host rather than one per project.
    """

    #: The registered project directory, verbatim from the registry.
    project: Path
    #: The ``claude-agent-<name>`` tag that project's next launch would use.
    image: str
    #: The rendered reason line, the same prose the cwd's own rows carry.
    reason: str


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
