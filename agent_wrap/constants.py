# This file has been edited with the assistance of an AI tool.
import os
from enum import Enum, auto
from pathlib import Path
from typing import Final

from agent_wrap.lib.daytime import local_utc_offset_hours, utc_offset_hours_for_tz

# Minimum sys.argv length for a valid CLI invocation (program name + verb).
MIN_ARGS = 2


class PollResult(Enum):
    """Verdict a poll callback returns each tick to ``DisplayService.poll_until``."""

    PENDING = auto()
    SUCCESS = auto()
    FAILURE = auto()


TOOL_DIR = Path(__file__).parent.parent.resolve()
GLOBAL_CONFIG_DIR = TOOL_DIR / ".claude_config"
AGENT_LAUNCHES_DIR = TOOL_DIR / ".agent-launches"
OPS_DIR = TOOL_DIR / "ops"

# Genuine strings (not paths)
BASE_IMAGE_NAME = "claude-agent"

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
    "ghcr.io/berriai/litellm:v1.83.14-stable"
    "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
)
TELEGRAM_IMAGE = (
    "mayty/claude-agent-wrap-telegram:0.2.0"
    "@sha256:db00b47cf61c4a59d436e016039ea0184a0f07ad6c68ba9e42db242f6dce2898"
)

# ── logs viewer ──────────────────────────────────────────────────────────────

# Env var name used by the detached logs-viewer child process to find the same
# tool_dir (and thus state file) as the parent that launched it.
LOGS_TOOL_DIR_ENV = "AGENT_LOGS_TOOL_DIR"

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

# ── run ──────────────────────────────────────────────────────────────────────

# In-container mount point for the agent-wrap ops directory.
AGENT_WRAP_MOUNT = "/opt/agent-wrap"

# Per-project state files mounted into the agent container.
STATE_FILES = (
    "daemon.lock",
    "daemon.log",
    "daemon.status.json",
    "history.jsonl",
)

# ── display / sidecars ────────────────────────────────────────────────────────

# Sentinel marking a horizontal divider in a table body list. Typed Final so it
# narrows to the Literal that ``RowItemOrDivider`` (display/models.py) expects.
DIVIDER: Final = "__div__"

#: Docker label name used to identify agent containers.
ROLE_LABEL = "agent-wrap.role"
#: Docker label value identifying agent containers.
ROLE_VALUE = BASE_IMAGE_NAME

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
