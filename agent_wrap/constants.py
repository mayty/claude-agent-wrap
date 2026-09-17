# This file has been edited with the assistance of an AI tool.
import os
import re
from enum import Enum, auto
from pathlib import Path
from typing import Final

from agent_wrap.lib.daytime import local_utc_offset_hours, utc_offset_hours_for_tz
from agent_wrap.lib.utils import is_truthy_env

#: Click ``context_settings`` for the root group, inherited by every subcommand. ``agent
#: run`` is the one exception -- it adds no help option at all, so ``--help`` reaches
#: Claude Code.
CLI_CONTEXT_SETTINGS: Final[dict[str, list[str]]] = {"help_option_names": ["-h", "--help"]}


class PollResult(Enum):
    PENDING = auto()
    SUCCESS = auto()
    FAILURE = auto()


class UpdateCheck(Enum):
    """
    Here rather than in the updates subpackage: ``launch`` and ``build`` both branch on
    it, and a runtime cross-domain import would trip EA001.
    """

    #: Nothing to update, or the user declined — run the original command.
    PROCEED = auto()
    #: An update ran; the caller's command is now stale and must not run. Exit 0.
    HANDLED = auto()
    #: Containers are live, so the update was refused outright. Exit 1.
    BLOCKED = auto()


class BuildForce(Enum):
    """
    Here rather than in the build subpackage, for the same EA001 reason as ``UpdateCheck``:
    ``launch`` names a member when asking for the images it will run.
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

# Exported to per-project startup scripts as ``AGENT_BINARY``, so they can call wrapper
# verbs without relying on the host's PATH or on agent-wrap.bashrc having been sourced.
AGENT_BINARY_PATH = TOOL_DIR / "bin" / "agent"

# ``bin/agent`` execs ``PYTHON_DIR / <current-venv> / bin/python3`` -- the venv, not the
# bare tree, because that is where the locked dependencies live. Both pointers are
# one-line text files rather than symlinks (see bin/agent-bootstrap for why). The venv
# directory name encodes the SHA-256 of the constraints it was built from, so a dependency
# change publishes a new directory instead of mutating the live one; the contributor venv
# ends in ``-dev`` instead (see DEV_VENV_SUFFIX in the status domain). None of this is
# mounted into a container.
AGENT_BOOTSTRAP_PATH = TOOL_DIR / "bin" / "agent-bootstrap"
PYTHON_PIN_FILE = TOOL_DIR / "python-pin.env"
PYTHON_CONSTRAINTS_FILE = TOOL_DIR / "bin" / "requirements.txt"
PYTHON_DIR = TOOL_DIR / ".python"
PYTHON_POINTER_FILE = PYTHON_DIR / "current"
PYTHON_VENV_POINTER_FILE = PYTHON_DIR / "current-venv"

BASE_IMAGE_NAME = "claude-agent"

# Bumped by hand once per release whose change to the base image's recipe must invalidate
# every such image on disk -- see CLAUDE.md, "Development workflow", for when that is. The
# value travels twice: as BUILD_ITERATION_LABEL it is how a host detects its base image is
# behind, and as the BUILD_ITERATION build arg it is the only thing that forces
# ops/Dockerfile's cached scaffold stage (apt, NodeSource, hadolint, crane) to refetch.
DOCKER_BUILD_ITERATION = 3

# In AGENT_LAUNCHES_DIR. Appended to on every `agent run`; read by `agent stats` and the
# logs viewer.
PROJECT_REGISTRY_FILENAME = "projects.txt"

# Sidecars write to ``<tool_dir>/litellm-logs/<project_hash>/<provider>/<session>/``; each
# project's ``.claude/litellm-logs`` is a symlink into its own slice.
LITELLM_LOGS_DIRNAME = "litellm-logs"

# What the sidecar's StringHasher writes into a record in place of a long string; the 64
# hex characters after it are the SHA-256 of the original, and therefore already the
# content address the blob store uses. At the package root because three layers need it:
# the ingester, the session stream, and the blob store's reachability sweep.
HASH_POINTER_PREFIX = "hash:"

# A whole pointer, for finding them in text rather than by walking parsed values -- both
# readers hold a blob as canonical JSON, where a pointer is 69 characters inside a string.
HASH_POINTER_RE = re.compile(HASH_POINTER_PREFIX + "[0-9a-f]{64}")

# How many successive ports a bind attempt probes before giving up.
PORT_SCAN_LIMIT = 50

LITELLM_IMAGE = (
    "ghcr.io/berriai/litellm:v1.96.2"
    "@sha256:154e23bb5f31b1f10e16392a8ef299bd2cde08de3a64a6849002cfcc25ce3c63"
)
TELEGRAM_IMAGE = (
    "mayty/claude-agent-wrap-telegram:0.2.0"
    "@sha256:db00b47cf61c4a59d436e016039ea0184a0f07ad6c68ba9e42db242f6dce2898"
)


# Lets the detached logs-viewer child find the same tool_dir (and state file) as its parent.
LOGS_TOOL_DIR_ENV = "AGENT_LOGS_TOOL_DIR"

# Opt-out for starting the logs viewer on `agent run`. Absent or empty means unset, which
# is on -- exporting the var with no value reads as clearing it, not as turning it off.
AUTOSTART_LOGS_ENV = "AGENT_AUTOSTART_LOGS"

LOGS_DEFAULT_PORT = 8765
LOGS_MIN_PORT = 1
LOGS_MAX_PORT = 65535

LOGS_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}


# AGENT_DAY_START_UTC must fall strictly within (-HOURS_PER_DAY, HOURS_PER_DAY).
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


# Hours past UTC midnight at which a stats "day" begins. Defaults to the host's local
# midnight, or AGENT_TIMEZONE's; override either with AGENT_DAY_START_UTC.
DAY_START_HOURS = _parsed_day_start_hours()

# The one usage source with behaviour attached: a successful request whose usage was never
# recorded contributes zero tokens and $0, so `agent stats` footnotes the count instead of
# letting the cost read as complete.
UNRECOVERABLE_SOURCE = "unrecoverable"

ORPHANED_LABEL = "<orphaned>"


# Checked into the project, unlike the git-ignored ``.claude/`` state tree next to it. A
# ``.gitignore`` pattern of ``.claude/`` does not match it, but a looser ``.claude*`` would.
AGENT_ASSETS_DIR = ".claude-agent-wrap"

# Named plainly so every editor, linter and highlighter recognizes the format.
AGENT_DOCKERFILE_NAME = "Dockerfile"

# Pre-0.10.0 location: ``<project>/Dockerfile.agent``. Still honored, with a deprecation
# warning on every use.
LEGACY_AGENT_DOCKERFILE_NAME = "Dockerfile.agent"

# Optional host-side script run before launch, gated by ``# agent-enable-startup:``.
AGENT_STARTUP_SCRIPT_NAME = "startup.sh"


AGENT_WRAP_MOUNT = "/opt/agent-wrap"

# Anything a project Dockerfile mounts *below* this path needs its mountpoint pre-created
# on the host, or docker materializes it inside the user's project as root -- see
# ``ConfigService.prepare_declared_mounts``.
WORKSPACE_MOUNT = "/workspace"

# Opt-out for the working-directory safeguard on `agent run` -- see SYSTEM_CWD_GLOBS in
# ``agent_wrap/domain/launch/constants.py``. Truthy per ``is_truthy_env``.
SKIP_SAFETY_CHECK_ENV = "AGENT_SKIP_SAFETY_CHECK"

# Only append-only files belong here: a single-file bind mount pins the inode, so any
# writer that replaces the file via rename() -- or unlinks it -- fails with EBUSY. Claude
# Code's PID-keyed daemon state is per-container instead; see INSTANCE_STATE_FILES in
# ``agent_wrap/domain/launch/constants.py``.
STATE_FILES = ("history.jsonl",)


# Unset means on; an explicitly falsy value turns it off. Explicit beats the settings file
# either way -- see ``ConfigService._ensure_spellcheck``.
SPELLCHECK_ENV = "AGENT_SPELLCHECK"

# One var feeds both the container env and the `docker build --build-arg`, because the two
# must agree: a ``language`` naming a dictionary that was never installed makes hunspell
# fail to start, silently disabling spell checking for the whole session.
SPELLCHECK_LANG_ENV = "AGENT_SPELLCHECK_LANG"
SPELLCHECK_BUILD_ARG = "SPELLCHECK_LANG"

# Pinned rather than left on Claude Code's "auto": auto prefers aspell when present, and
# aspell's --lang takes a single dictionary, so a comma-separated list would stop working.
SPELLCHECK_CHECKER = "hunspell"

# hunspell accepts a word found in any loaded dictionary, which is what makes a mixed
# English/Russian prompt check cleanly.
DEFAULT_SPELLCHECK_LANG = "en_US,ru_RU"

# Deliberately stricter than Claude Code's own validator: the value is interpolated into a
# `docker build` shell step and into apt package names.
SPELLCHECK_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(_[A-Za-z]{2,})?$")

# Claude Code drops a `language` longer than this, leaving no dictionary in force.
SPELLCHECK_LANG_MAX_LEN = 64


def _parsed_spellcheck_enabled() -> bool | None:
    """
    Tri-state AGENT_SPELLCHECK: None when unset, else its truthiness.

    An empty value counts as unset, matching every other AGENT_* flag here.
    """
    raw = os.environ.get(SPELLCHECK_ENV, "")
    if not raw:
        return None
    return is_truthy_env(raw)


def _parsed_spellcheck_lang() -> str | None:
    """
    Normalise AGENT_SPELLCHECK_LANG to a comma-separated list, or None when unset.

    Raises on a malformed value rather than falling back to the default: substituting it
    would install one set of dictionaries and configure another, and the mismatch surfaces
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


#: None when unset, else the state it asks for, overriding the settings file every launch.
SPELLCHECK_ENABLED_OVERRIDE = _parsed_spellcheck_enabled()

#: None when unset, else the normalised list it asks for.
SPELLCHECK_LANG_OVERRIDE = _parsed_spellcheck_lang()

SPELLCHECK_LANG = SPELLCHECK_LANG_OVERRIDE or DEFAULT_SPELLCHECK_LANG


#: Sentinel marking a horizontal divider in a table body list. Typed Final so it narrows to
#: the Literal that ``RowItemOrDivider`` (display/models.py) expects.
DIVIDER: Final = "__div__"

ROLE_LABEL = "agent-wrap.role"
ROLE_VALUE = BASE_IMAGE_NAME
#: The flock registry's key, and what the stale per-instance state sweep matches live
#: containers on.
INSTANCE_ID_LABEL = "agent-wrap.instance-id"

#: DOCKER_BUILD_ITERATION as of the build. Stamped on every image the wrapper builds, but
#: read only off the *base* image: docker merges Config.Labels through FROM, so a project
#: image's copy is inherited and says nothing about the project image itself.
BUILD_ITERATION_LABEL = "agent-wrap.build-iteration"
#: The base image's docker Id as of the project build. Absent means the image predates
#: stamping and has to be rebuilt once.
BASE_IMAGE_ID_LABEL = "agent-wrap.base-image-id"
#: The tag an image was built as. Unlike the two labels above its value is *rewritten* on
#: every wrapper build, which makes it the only usable handle on a *superseded* build:
#: docker takes the repository as well as the tag away when an image loses it. Presence
#: still proves nothing about ownership -- Config.Labels merge through FROM.
IMAGE_NAME_LABEL = "agent-wrap.image"

LITELLM_SIDECAR_LABEL = "litellm-sidecar"
TELEGRAM_SIDECAR_LABEL = "telegram-sidecar"
TELEGRAM_SIDECAR_NAME = "telegram"

# Docker's own state string for a container that is up. A container that is not running
# has no uptime -- docker keeps reporting StartedAt (its last start) for a stopped one,
# which would read as the age of a corpse.
RUNNING_STATUS = "running"

# Health reported for a container declaring no health check. The Telegram sidecar is
# started without --health-cmd, unlike the LiteLLM one, so this is a fact not a problem.
NO_HEALTHCHECK = "none"

# Joined by every sidecar and agent, which is what gives the agent container DNS resolution
# for the sidecar's name. Docker's default bridge has no embedded DNS.
SIDECAR_NETWORK_NAME = "agent-wrap-net"

DEFAULT_PROVIDER_NAME = "litellm-bedrock"

# A provider's sidecar is f"{CONTAINER_NAME_PREFIX}-{provider.name}", which is what makes
# concurrent per-provider sidecars possible. Agent containers deliberately do NOT share
# this prefix (they are "claude-agent-<instance_id>"), so it alone selects sidecars.
CONTAINER_NAME_PREFIX = "agent-wrap"

# The running container is the single source of truth for the port it resolved at cold
# start: later launches recover it from here rather than re-scanning, which would pick a
# different port and break connectivity.
SIDECAR_PORT_ENV = "AGENT_WRAP_SIDECAR_PORT"

# Fixed for the container's lifetime (one container per provider), so the callback reads
# the provider from the container env rather than per-request.
SIDECAR_PROVIDER_ENV = "AGENT_WRAP_PROVIDER"

# The size `Core.console_render` is built at. Stated rather than probed: a table's columns
# were negotiated against the terminal before reaching rich, and a self-sizing console
# would collapse them again. Both halves are needed -- rich only honours a width when a
# height accompanies it, and answers 80x25 for a dumb terminal otherwise.
RENDER_CONSOLE_SIZE = (10_000, 10_000)
