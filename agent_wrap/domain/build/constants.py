# This file has been created with the assistance of an AI tool.
"""Constants for the build domain."""

import re
from enum import Enum, auto

from agent_wrap.constants import BASE_IMAGE_NAME, LITELLM_IMAGE, TELEGRAM_IMAGE

# Seconds a project startup script may run when ``# agent-enable-startup:`` is given a
# plain boolean. Deliberately short: the script runs while holding the host-global
# sidecar lock, so every concurrently launching agent waits behind it. A project that
# genuinely needs longer states its own budget (``# agent-enable-startup: 45``).
DEFAULT_STARTUP_TIMEOUT_SECONDS = 10.0

# Directive values that mean "on, with the default timeout" and "off".
STARTUP_TRUTHY_WORDS = frozenset({"true", "yes", "on"})
STARTUP_FALSY_WORDS = frozenset({"false", "no", "off"})

# A ``FROM`` line, and the subset of them that names the wrapper's base image. Any tag
# reads as the base: the wrapper only ever builds it untagged, so a tagged spelling can
# only mean the same image, and the id comparison downstream is against that one build.
FROM_RE = re.compile(r"^[Ff][Rr][Oo][Mm]\s+(\S+)")
BASE_FROM_RE = re.compile(rf"^{BASE_IMAGE_NAME}(:.*)?$")

# The namespace every project image is tagged into. A local repository belongs to the
# wrapper when it equals BASE_IMAGE_NAME or starts with this and carries no "/" -- the
# wrapper never tags into a registry, so a slash means the image is somebody else's.
WRAPPER_IMAGE_PREFIX = f"{BASE_IMAGE_NAME}-"

# The sidecar images the wrapper pulls, pinned tag+digest. Read here so `agent cleanup`
# can tell a superseded pull from the live one; both live in the root constants because
# launch, sidecars and providers all name them too.
PINNED_SIDECAR_IMAGES = (LITELLM_IMAGE, TELEGRAM_IMAGE)

# Host-global lock file, under AGENT_LAUNCHES_DIR, serializing image builds across every
# concurrently launching agent. Held across a whole `docker build`, which is why the
# staleness questions are asked *inside* it -- see BuildService.ensure_images.
BUILD_LOCK_NAME = "build.lock"

# Build args that steer the base image's layer cache, both consumed by ops/Dockerfile.
# BUILD_ITERATION invalidates the cached `scaffold` stage when the wrapper says its recipe
# moved; CLAUDE_CACHE_BUST carries a value that differs on every build, which is what
# keeps the Claude Code CLI layer out of the cache. Both are *referenced* by a RUN in the
# Dockerfile rather than merely declared: BuildKit invalidates on an arg's first use, the
# classic builder on its declaration, and only referencing them behaves the same on both.
BUILD_ITERATION_BUILD_ARG = "BUILD_ITERATION"
CLAUDE_CACHE_BUST_BUILD_ARG = "CLAUDE_CACHE_BUST"

# The note appended to the "reason:" line, saying what an auto-build is about to cost.
# One per image kind, because the honest answer differs: the base image builds with
# docker's layer cache on, so its scaffolding re-runs only when the recipe moved, while a
# project image still builds with --no-cache and re-runs everything. Deliberately not
# tailored per reason -- only ITERATION_CHANGED guarantees a cold scaffold, since a
# deleted or pre-stamping image leaves the build cache itself intact.
BASE_BUILD_CACHE_NOTE = (
    "its scaffolding layers are reused from the docker cache when they are still current, "
    "and only the Claude Code CLI is always reinstalled -- minutes if the cache is cold"
)
PROJECT_BUILD_CACHE_NOTE = "this build runs with --no-cache and re-runs every RUN step"


class BuildReason(Enum):
    """Why an image is about to be built."""

    #: No image by that name exists on this host.
    MISSING = auto()
    #: Present, but carries no wrapper build stamp -- built before stamping existed.
    UNSTAMPED = auto()
    #: The base image's stamped build iteration is not the one this code carries.
    ITERATION_CHANGED = auto()
    #: A project image whose base image has been rebuilt since.
    BASE_CHANGED = auto()
    #: The caller asked for an unconditional rebuild (``agent rebuild``).
    FORCED = auto()


# The "reason:" line printed under an auto-build banner, formatted with ``image``,
# ``base``, ``was`` and ``now``. FORCED is absent on purpose: an explicit
# ``agent rebuild`` needs no explanation, and the banner alone is what it printed before.
BUILD_REASON_TEXT = {
    BuildReason.MISSING: "it is not built on this host",
    BuildReason.UNSTAMPED: (
        "it was built before agent-wrap stamped its images, so it cannot be checked -- "
        "this is a one-time rebuild after the upgrade"
    ),
    BuildReason.ITERATION_CHANGED: (
        "the wrapper's docker build iteration changed (image {was}, current {now})"
    ),
    BuildReason.BASE_CHANGED: "the base image {base} is not the one it was built on",
}


class ImageCleanupReason(Enum):
    """Why an image is no longer needed and ``agent cleanup`` offers to remove it."""

    #: An untagged wrapper build, left behind by a later build of the same tag.
    SUPERSEDED = auto()
    #: A ``claude-agent-<name>`` tag no registered project resolves to any more.
    ORPHANED = auto()
    #: A project image that would be rebuilt on that project's next launch anyway.
    STALE = auto()
    #: A pulled sidecar image that is not the digest the wrapper pins.
    SUPERSEDED_SIDECAR = auto()


# The "WHY" column of the `agent cleanup` preview table, formatted with ``detail`` --
# whichever of the superseded tag name, the project path, or the staleness prose that
# reason carries on its RemovableImage.
IMAGE_CLEANUP_REASON_TEXT = {
    ImageCleanupReason.SUPERSEDED: "superseded build of {detail}",
    ImageCleanupReason.ORPHANED: "no registered project builds it",
    ImageCleanupReason.STALE: "{detail}",
    ImageCleanupReason.SUPERSEDED_SIDECAR: "not the pinned digest for {detail}",
}

# Header for each group of rows in that table, formatted with ``count``. Separate from the
# prose above because a group heading counts and a row explains. Kept short deliberately:
# a heading sits in the elidable IMAGE column, so anything that must be read in full --
# the rebuild a stale removal costs -- goes in a note under the table instead.
IMAGE_CLEANUP_GROUP_TEXT = {
    ImageCleanupReason.SUPERSEDED: "{count} superseded build(s)",
    ImageCleanupReason.ORPHANED: "{count} orphaned project image(s)",
    ImageCleanupReason.STALE: "{count} stale project image(s)",
    ImageCleanupReason.SUPERSEDED_SIDECAR: "{count} superseded sidecar image(s)",
}

# Go templates for the three `docker image ls` listings the image sweep runs. Tab-separated
# because a docker-rendered size ("1.23GB") contains no tab, and neither do ids or names.
TAGGED_IMAGE_TEMPLATE = "{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}"
UNTAGGED_IMAGE_TEMPLATE = "{{.ID}}\t{{.Size}}"
SIDECAR_IMAGE_TEMPLATE = "{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Digest}}\t{{.Size}}"

# Field counts the three templates above render, asserted before unpacking so a docker
# version that renders something unexpected drops the row instead of raising.
TAGGED_IMAGE_FIELDS = 4
UNTAGGED_IMAGE_FIELDS = 2
SIDECAR_IMAGE_FIELDS = 5

# Docker's stand-in for a field an image does not have -- an untagged image's tag, or the
# digest of one that was never pulled from a registry. Never a value to compare against.
DOCKER_NONE = "<none>"

# How many hex characters `docker image ls` renders for an image id. `docker image inspect`
# answers with the full "sha256:..." form instead, so both sides are keyed on this length
# when a listing has to be joined to an inspect.
SHORT_IMAGE_ID_LEN = 12
