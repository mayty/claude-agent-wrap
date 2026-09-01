# This file has been edited with the assistance of an AI tool.
"""Data models for the build domain."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.build.constants import BuildReason, ImageCleanupReason


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


class ProjectImageVerdict(NamedTuple):
    """
    One registered project, the image its next launch targets, and that image's verdict.

    The raw output of the per-project sweep, before anything decides what to *do* with it:
    :meth:`BuildService.stale_project_images` renders the non-current ones as a report,
    while :meth:`BuildService.image_cleanup_scope` also needs the current ones — those are
    exactly the tags that are claimed and must not be read as orphaned.
    """

    #: The registered project directory, verbatim from the registry.
    project: Path
    #: The image that project's next launch would use.
    image: str
    #: Why it would be rebuilt, or None when it is current.
    reason: BuildReason | None


class RemovableImage(NamedTuple):
    """One image ``agent cleanup`` offers to remove, ready to preview and to delete."""

    #: What ``docker rmi`` is given: the id for an untagged image, ``repo:tag`` for a
    #: wrapper image, ``repo@sha256:...`` for a sidecar one pinned by digest.
    ref: str
    #: How the image is named in the preview, which for an untagged one is its short id.
    display: str
    #: Docker's own id, carried separately so two rows can be told apart when both are
    #: untagged and neither has a name to show.
    image_id: str
    #: Docker's own rendered size ("1.23GB"), shown per row and deliberately never summed.
    size: str
    #: Which of the four kinds of outdated this is.
    reason: ImageCleanupReason
    #: The reason's one variable part -- a superseded build's recorded tag, the project
    #: path behind an orphan, the staleness prose, or the pinned sidecar reference.
    detail: str


class ImageCleanupScope(NamedTuple):
    """
    What an image cleanup would remove, surveyed before anything is deleted.

    *unattributable* counts untagged images carrying no ``agent-wrap.image`` label: built
    before the wrapper stamped its images with their own name, so nothing can prove they
    are the wrapper's. They are never removed, only counted, so the summary can point at
    ``docker image prune`` once instead of leaving the disk they hold unexplained.
    """

    images: list[RemovableImage]
    unattributable: int

    @property
    def is_empty(self) -> bool:
        """Whether there is no image to remove. An unattributable count is not one."""
        return not self.images


class ImageCleanupOutcome(NamedTuple):
    """
    What an image cleanup actually did.

    *skipped* holds the images docker refused to remove, which is almost always one a
    running container still references — ``remove_image`` never forces, so that refusal
    reaches here as a row to report rather than an image to lose.
    """

    removed: list[RemovableImage]
    skipped: list[RemovableImage]


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
