# This file has been edited with the assistance of an AI tool.
import os
import re
from enum import Enum, auto
from pathlib import Path
from typing import Final

from agent_wrap.lib.daytime import local_utc_offset_hours, utc_offset_hours_for_tz
from agent_wrap.lib.utils import is_truthy_env

# Minimum sys.argv length for a valid CLI invocation (program name + verb).
MIN_ARGS = 2


class PollResult(Enum):
    """Verdict a poll callback returns each tick to ``DisplayService.poll_until``."""

    PENDING = auto()
    SUCCESS = auto()
    FAILURE = auto()


class UpdateCheck(Enum):
    """
    What ``UpdateService.check_updates`` decided, for the command that asked to act on.

    Lives here rather than in the updates subpackage because ``launch`` and ``build``
    both branch on it, and a runtime cross-domain import would trip rule EA001.
    """

    #: Nothing to update, or the user declined — run the original command.
    PROCEED = auto()
    #: An update ran; the caller's command is now stale and must not run. Exit 0.
    HANDLED = auto()
    #: Containers are live, so the update was refused outright. Exit 1.
    BLOCKED = auto()


class BuildForce(Enum):
    """
    What ``BuildService.ensure_images`` must build regardless of staleness.

    Lives here rather than in the build subpackage for the same reason as
    ``UpdateCheck``: ``launch`` has to name a member when it asks for the images it is
    about to run, and a runtime cross-domain import would trip rule EA001.
    """

    #: ``agent run`` — build only what is missing or stale.
    NONE = auto()
    #: ``agent rebuild`` — always rebuild the project image; ensure the base.
    PROJECT = auto()
    #: ``agent rebuild --full`` — always rebuild both.
    ALL = auto()


TOOL_DIR = Path(__file__).parent.parent.resolve()
GLOBAL_CONFIG_DIR = TOOL_DIR / ".claude_config"
AGENT_LAUNCHES_DIR = TOOL_DIR / ".agent-launches"
OPS_DIR = TOOL_DIR / "ops"

# The `agent` entry point itself, exported to per-project startup scripts as
# ``AGENT_BINARY`` so they can call wrapper verbs without relying on the host's PATH
# or on ``agent-wrap.bashrc`` having been sourced.
AGENT_BINARY_PATH = TOOL_DIR / "bin" / "agent"

# The provisioned CPython. ``bin/agent`` execs ``PYTHON_DIR / <pointer> / bin/python3``,
# where the pointer is a one-line text file rather than a symlink (see bin/agent-bootstrap
# for why). None of this is ever mounted into a container: _build_volume_mounts exposes
# only OPS_DIR and the .claude_config/.claude state dirs.
AGENT_BOOTSTRAP_PATH = TOOL_DIR / "bin" / "agent-bootstrap"
PYTHON_PIN_FILE = TOOL_DIR / "python-pin.env"
PYTHON_DIR = TOOL_DIR / ".python"
PYTHON_POINTER_FILE = PYTHON_DIR / "current"

# Genuine strings (not paths)
BASE_IMAGE_NAME = "claude-agent"

# Bumped by hand when a change to the *base image's* recipe has to invalidate every such
# image already on disk -- ops/Dockerfile, or the build args _docker_build passes. `agent
# run` compares this against BUILD_ITERATION_LABEL on the local base image and rebuilds it
# -- and every project image on top of it -- when the two differ.
#
# Scope is the wrapper as a tool, on every host that runs it. A change to one project's
# own .claude-agent-wrap/Dockerfile, this repo's included, is not a reason to bump: that
# image is rebuilt by an `agent rebuild` in that project. One bump per release is enough,
# and nothing enforces it: not every base-affecting change is statically detectable. See
# CLAUDE.md, "Development workflow".
#
# The value travels twice, and both trips matter. As BUILD_ITERATION_LABEL it is how a
# host *detects* that its base image is behind; as the BUILD_ITERATION build arg it is how
# a bump *reaches* the cached `scaffold` stage of ops/Dockerfile. Because the base builds
# with docker's layer cache on, a bump is the only thing that forces its apt, NodeSource,
# hadolint and crane layers to be fetched again.
DOCKER_BUILD_ITERATION = 2

# Filename of the project registry that `agent run` appends to on every launch, and
# that `agent stats` / the logs viewer read. Lives in AGENT_LAUNCHES_DIR.
PROJECT_REGISTRY_FILENAME = "projects.txt"

# Directory name of the shared per-project request logs. Sidecars write to
# ``<tool_dir>/litellm-logs/<project_hash>/<provider>/<session>/``; each project's
# ``.claude/litellm-logs`` is a symlink into its own slice.
LITELLM_LOGS_DIRNAME = "litellm-logs"

# How many successive ports a bind attempt probes before giving up. Shared by the
# sidecar cold start and the logs viewer.
PORT_SCAN_LIMIT = 50

# Pinned sidecar Docker images (tag + digest)
LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm:v1.96.2"
    "@sha256:154e23bb5f31b1f10e16392a8ef299bd2cde08de3a64a6849002cfcc25ce3c63"
)
TELEGRAM_IMAGE = (
    "mayty/claude-agent-wrap-telegram:0.2.0"
    "@sha256:db00b47cf61c4a59d436e016039ea0184a0f07ad6c68ba9e42db242f6dce2898"
)

# ── logs viewer ──────────────────────────────────────────────────────────────

# Env var name used by the detached logs-viewer child process to find the same
# tool_dir (and thus state file) as the parent that launched it.
LOGS_TOOL_DIR_ENV = "AGENT_LOGS_TOOL_DIR"

# Opt-out for starting the logs viewer on `agent run`. Absent or empty means unset, which
# is on -- exporting the var with no value reads as clearing it, not as asking for the
# feature to be off. Read by the launcher that acts on it and by `agent inspect`, which
# reports whether it is actually in effect.
AUTOSTART_LOGS_ENV = "AGENT_AUTOSTART_LOGS"

# Port range for the local web viewer.
LOGS_DEFAULT_PORT = 8765
LOGS_MIN_PORT = 1
LOGS_MAX_PORT = 65535

# Extension → Content-Type mapping for the static file server.
LOGS_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# ── stats ────────────────────────────────────────────────────────────────────

# Hours in a day -- AGENT_DAY_START_UTC must fall strictly within (-HOURS_PER_DAY, HOURS_PER_DAY).
HOURS_PER_DAY = 24


def _parsed_day_start_hours() -> int:
    raw = os.environ.get("AGENT_DAY_START_UTC")
    if raw:
        value = int(raw)  # raises ValueError on malformed input -- let it propagate
        if abs(value) >= HOURS_PER_DAY:
            msg = f"AGENT_DAY_START_UTC must satisfy -24 < value < 24, got {value!r}"
            raise ValueError(msg)
        return value
    tz_name = os.environ.get("AGENT_TIMEZONE")
    if tz_name:
        return -utc_offset_hours_for_tz(tz_name)  # raises on an unknown zone -- let it propagate
    return -local_utc_offset_hours()


# Hours past UTC midnight at which a stats "day" begins (may be negative, but
# must satisfy -24 < value < 24). Defaults to the host's local midnight, or
# AGENT_TIMEZONE's midnight if set; override either with AGENT_DAY_START_UTC.
DAY_START_HOURS = _parsed_day_start_hours()

# Recognised usage-source tags stamped onto records by the callback.
USAGE_SOURCES = ("native", "standard_logging_object", "unrecoverable")

# Display label for orphaned sessions — logs from deleted or unregistered projects
# that no longer have an entry in the project registry.
ORPHANED_LABEL = "<orphaned>"

# Files below this count are scanned serially (fork overhead > benefit).
SCAN_PARALLEL_MIN_FILES = 64

# ── project agent assets ─────────────────────────────────────────────────────

# Per-project wrapper assets live in this directory, checked into the project (unlike
# the git-ignored ``.claude/`` state tree next to it). Note that a ``.gitignore``
# pattern of ``.claude/`` does not match it, but a looser ``.claude*`` would.
AGENT_ASSETS_DIR = ".claude-agent-wrap"

# The project Dockerfile. Named plainly so every editor, linter and highlighter
# recognizes the format -- which the legacy name below defeated.
AGENT_DOCKERFILE_NAME = "Dockerfile"

# Pre-0.10.0 project Dockerfile location: ``<project>/Dockerfile.agent``. Still
# honored, with a deprecation warning on every use.
LEGACY_AGENT_DOCKERFILE_NAME = "Dockerfile.agent"

# Optional host-side script run before launch, gated by ``# agent-enable-startup:``.
AGENT_STARTUP_SCRIPT_NAME = "startup.sh"


# ── run ──────────────────────────────────────────────────────────────────────

# In-container mount point for the agent-wrap ops directory.
AGENT_WRAP_MOUNT = "/opt/agent-wrap"

# In-container mount point for the project directory. Anything a project Dockerfile
# mounts *below* this path needs its mountpoint pre-created on the host, or docker
# materializes it inside the user's project as root -- see
# ``ConfigService.prepare_declared_mounts``.
WORKSPACE_MOUNT = "/workspace"

# Per-project state files mounted into the agent container. Only append-only files
# belong here: a single-file bind mount pins the inode, so any writer that replaces
# the file via rename() -- or unlinks it -- fails with EBUSY. Claude Code's PID-keyed
# daemon state is per-container instead; see INSTANCE_STATE_FILES in
# ``agent_wrap/domain/launch/constants.py``.
STATE_FILES = ("history.jsonl",)

# ── spell checking ───────────────────────────────────────────────────────────

# Env var gating Claude Code's prompt spell checking. Unset means on; an explicitly
# falsy value turns it off. Explicit beats the settings file either way -- see
# ``ConfigService._ensure_spellcheck``.
SPELLCHECK_ENV = "AGENT_SPELLCHECK"

# Env var carrying the dictionary list, and the `docker build --build-arg` that hands the
# same value to ops/Dockerfile. One var feeds both because the two must agree: a
# ``language`` naming a dictionary that was never installed makes hunspell fail to start,
# which silently disables spell checking for the whole session.
SPELLCHECK_LANG_ENV = "AGENT_SPELLCHECK_LANG"
SPELLCHECK_BUILD_ARG = "SPELLCHECK_LANG"

# The checker written into the settings block. Pinned rather than left on Claude Code's
# "auto": auto prefers aspell when it is present, and aspell's --lang takes a single
# dictionary, so a comma-separated list would quietly stop working.
SPELLCHECK_CHECKER = "hunspell"

# Dictionaries loaded together. hunspell accepts a word found in any of them, which is
# what makes a mixed English/Russian prompt check cleanly.
DEFAULT_SPELLCHECK_LANG = "en_US,ru_RU"

# One dictionary name. Deliberately stricter than Claude Code's own validator: the value
# is interpolated into a `docker build` shell step and into apt package names.
SPELLCHECK_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z]{2,})?$")

# Claude Code drops a `language` longer than this, leaving no dictionary in force.
SPELLCHECK_LANG_MAX_LEN = 64


def _parsed_spellcheck_enabled() -> bool | None:
    """
    Tri-state AGENT_SPELLCHECK: None when unset, else its truthiness.

    An empty value counts as unset, matching every other AGENT_* flag here -- exporting
    the var with no value reads as clearing it, not as asking for it to be off.
    """
    raw = os.environ.get(SPELLCHECK_ENV, "")
    if not raw:
        return None
    return is_truthy_env(raw)


def _parsed_spellcheck_lang() -> str | None:
    """
    Normalise AGENT_SPELLCHECK_LANG to a comma-separated list, or None when unset.

    Raises on a malformed value rather than falling back to the default -- the same
    philosophy as AGENT_DAY_START_UTC above. Silently substituting the default would
    install one set of dictionaries and configure another, and the mismatch surfaces
    only as spell checking being mysteriously off.
    """
    raw = os.environ.get(SPELLCHECK_LANG_ENV, "")
    if not raw.strip():
        return None
    langs = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if not langs:
        msg = f"{SPELLCHECK_LANG_ENV} must name at least one dictionary, got {raw!r}"
        raise ValueError(msg)
    for lang in langs:
        if not SPELLCHECK_LANG_RE.match(lang):
            msg = (
                f"{SPELLCHECK_LANG_ENV} entry {lang!r} is not a dictionary name "
                f"(expected e.g. 'en_US' or 'ru')"
            )
            raise ValueError(msg)
    joined = ",".join(langs)
    if len(joined) > SPELLCHECK_LANG_MAX_LEN:
        msg = (
            f"{SPELLCHECK_LANG_ENV} must be at most {SPELLCHECK_LANG_MAX_LEN} characters "
            f"once joined (Claude Code drops longer values), got {len(joined)}"
        )
        raise ValueError(msg)
    return joined


#: None when AGENT_SPELLCHECK is unset, else the state it asks for. An explicit value
#: overrides whatever the settings file holds, on every launch.
SPELLCHECK_ENABLED_OVERRIDE = _parsed_spellcheck_enabled()

#: None when AGENT_SPELLCHECK_LANG is unset, else the normalised list it asks for.
SPELLCHECK_LANG_OVERRIDE = _parsed_spellcheck_lang()

#: The dictionary list in force -- what the image installs and what a freshly injected
#: settings block names.
SPELLCHECK_LANG = SPELLCHECK_LANG_OVERRIDE or DEFAULT_SPELLCHECK_LANG

# ── display / sidecars ────────────────────────────────────────────────────────

# Sentinel marking a horizontal divider in a table body list. Typed Final so it
# narrows to the Literal that ``RowItemOrDivider`` (display/models.py) expects.
DIVIDER: Final = "__div__"

#: Docker label name used to identify agent containers.
ROLE_LABEL = "agent-wrap.role"
#: Docker label value identifying agent containers.
ROLE_VALUE = BASE_IMAGE_NAME
#: Docker label carrying an agent's instance id — the flock registry's key, and the
#: key the stale per-instance state sweep matches live containers on.
INSTANCE_ID_LABEL = "agent-wrap.instance-id"

#: Image label carrying DOCKER_BUILD_ITERATION as of the build. Stamped on every image the
#: wrapper builds, but read only off the *base* image: docker merges Config.Labels through
#: FROM, so a project image's copy is inherited from the base and says nothing about the
#: project image itself.
BUILD_ITERATION_LABEL = "agent-wrap.build-iteration"
#: Image label carrying the base image's docker Id as of the project build. An absent label
#: means the image predates stamping and has to be rebuilt once.
BASE_IMAGE_ID_LABEL = "agent-wrap.base-image-id"
#: Image label carrying the tag an image was built as -- "claude-agent" or
#: "claude-agent-<name>". Unlike the two labels above, its value is *rewritten* on every
#: wrapper build, so a wrapper image's copy always names itself rather than an ancestor.
#: That is what makes it the only usable handle on a *superseded* build: docker takes the
#: repository as well as the tag away when an image loses it, and `agent cleanup` has
#: nothing else left to match on. Presence still proves nothing about ownership -- docker
#: merges Config.Labels through FROM, so an image built on a wrapper image inherits it too.
IMAGE_NAME_LABEL = "agent-wrap.image"

LITELLM_SIDECAR_LABEL = "litellm-sidecar"
TELEGRAM_SIDECAR_LABEL = "telegram-sidecar"
TELEGRAM_SIDECAR_NAME = "telegram"

# Docker's own state string for a container that is up. Anything else means it is not,
# and a container that is not running has no uptime — docker keeps reporting StartedAt
# for a stopped container (its last start), which would read as the age of a corpse.
RUNNING_STATUS = "running"

# Health value reported for a container that declares no health check at all. The
# Telegram sidecar is started without --health-cmd, unlike the LiteLLM one, so this is
# a fact about the container rather than a problem.
NO_HEALTHCHECK = "none"

# User-defined Docker network every sidecar and agent joins, which is what gives the
# agent container DNS resolution for the sidecar's name. Docker's default bridge has no
# embedded DNS, so this cannot be replaced by it.
SIDECAR_NETWORK_NAME = "agent-wrap-net"

# Provider used when AGENT_PROVIDER is unset.
DEFAULT_PROVIDER_NAME = "litellm-bedrock"

# Prefix shared by every sidecar container. A provider's own sidecar is
# f"{CONTAINER_NAME_PREFIX}-{provider.name}" (e.g. "agent-wrap-litellm-bedrock"), which
# is what makes concurrent per-provider sidecars possible; the single Telegram sidecar
# is "agent-wrap-telegram". Agent containers deliberately do NOT share this prefix
# (they are "claude-agent-<instance_id>"), so the prefix alone selects sidecars.
CONTAINER_NAME_PREFIX = "agent-wrap"

# Container env var carrying the port a sidecar resolved at cold start. The running
# container is the single source of truth: later launches recover it from here rather
# than re-scanning (which would pick a different port and break connectivity).
SIDECAR_PORT_ENV = "AGENT_WRAP_SIDECAR_PORT"

# Container env var carrying the provider a LiteLLM sidecar serves. Fixed for the
# container's lifetime (one container per provider), so the callback reads it from the
# container env rather than per-request.
SIDECAR_PROVIDER_ENV = "AGENT_WRAP_PROVIDER"
