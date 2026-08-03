# This file has been edited with the assistance of an AI tool.
"""Docker-related utility functions."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent_wrap.lib.utils import is_truthy_env

# Docker emits RFC3339 with nanosecond precision and a literal "Z"
# (e.g. "2026-07-30T09:39:12.123456789Z"). Split off the fractional part so it can be
# truncated to the 6 digits datetime accepts.
_TIMESTAMP_RE = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?$"
)

# What docker reports for a timestamp that never happened (e.g. StartedAt on a
# container that was created but never started). It parses fine and would yield a
# ~2000-year uptime, so it is mapped to None instead.
_ZERO_TIMESTAMP_YEAR = 1

# Maximum fractional digits datetime.fromisoformat accepts.
_MAX_FRACTIONAL_DIGITS = 6


def is_wsl() -> bool:
    """Check if running on WSL (Microsoft kernel)."""
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def host_network_build_args() -> list[str]:
    """
    Return ["--network", "host"] for `docker build` when the WSL host-network
    workaround is active, else [].

    Honored only on WSL (see docs/configuration.md): the parallel-distro
    iptables-legacy FORWARD=DROP scenario that breaks `agent run` also breaks a
    build's `RUN` steps, which execute on Docker's default bridge.
    """
    if not is_wsl():
        return []
    if not is_truthy_env(os.environ.get("AGENT_USE_HOST_NETWORK", "")):
        return []
    return ["--network", "host"]


def docker_run(
    *args: str,
    capture: bool = True,
    timeout: int = 30,
) -> tuple[str, int]:
    """
    Run a docker command and return (stdout, returncode).

    On timeout, missing binary, or other subprocess errors, returns ("", 1).
    """
    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError):
        return "", 1
    stdout = result.stdout.strip() if result.stdout is not None else ""
    return stdout, result.returncode


def is_rootless() -> bool:
    """Check if Docker is running in rootless mode."""
    stdout, _ = docker_run("info", timeout=10)
    return "rootless" in stdout.lower()


def daemon_reachable() -> bool:
    """
    Report whether the Docker daemon answers at all.

    Used to tell "no containers match" apart from "docker is down", which look
    identical in a listing's empty output.
    """
    _, rc = docker_run("version", "--format", "{{.Server.Version}}", timeout=10)
    return rc == 0


def list_container_names(*filters: str) -> list[str]:
    """
    List names of containers matching every ``docker ps --filter`` expression given.

    Includes stopped containers (``-a``): a container that exited is exactly what a
    diagnostic listing wants to surface, and the caller can read its state separately.
    Returns [] when nothing matches or docker is unavailable — indistinguishable here
    on purpose, so callers that care use :func:`daemon_reachable`.
    """
    args = ["ps", "-a", "--format", "{{.Names}}"]
    for expr in filters:
        args.extend(["--filter", expr])
    stdout, rc = docker_run(*args)
    if rc != 0:
        return []
    return [line.strip() for line in stdout.splitlines() if line.strip()]


def inspect_containers(names: list[str], template: str) -> tuple[list[str], int]:
    """
    Batch-inspect *names* with a Go *template*, returning its output lines and the rc.

    ``container inspect`` — not plain ``inspect``, which falls back to matching an
    *image* of that name. One docker call for the whole batch; *template* must render
    each container on a single line (wrap every composite field in ``{{json .Field}}``,
    which escapes newlines and tabs) or the line-to-container correspondence breaks.

    The rc is returned rather than interpreted because a non-zero rc *with* output is
    routine: a container that disappeared between listing and inspection makes docker
    report an error for that name while still printing rows for the others. Only an
    empty result is a real failure, and even then the caller decides.
    """
    if not names:
        return [], 0
    stdout, rc = docker_run("container", "inspect", "--format", template, *names)
    return [line for line in stdout.splitlines() if line.strip()], rc


def parse_docker_timestamp(raw: str) -> datetime | None:
    """
    Parse a docker RFC3339 timestamp into a UTC-aware datetime, or None if unusable.

    Written out rather than handed to ``datetime.fromisoformat`` because the supported
    floor is Python 3.10, which accepts neither a trailing ``Z``, nor a colon-less UTC
    offset (``+0200``), nor docker's nanosecond precision (it wants exactly 3 or 6
    fractional digits). Fractional digits are truncated, not rounded — sub-microsecond
    precision is meaningless for the uptimes this feeds.

    Docker's zero timestamp (``0001-01-01T00:00:00Z``, meaning "never") returns None.
    """
    match = _TIMESTAMP_RE.match(raw.strip())
    if match is None:
        return None

    frac = (match.group("frac") or "")[:_MAX_FRACTIONAL_DIGITS]
    tz = match.group("tz") or "Z"
    if tz != "Z" and ":" not in tz:
        tz = f"{tz[:3]}:{tz[3:]}"
    normalized = match.group("base")
    if frac:
        normalized += f".{frac}"
    normalized += "+00:00" if tz == "Z" else tz

    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.year <= _ZERO_TIMESTAMP_YEAR:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def image_exists(image: str) -> bool:
    """Check if a Docker image exists locally."""
    _, rc = docker_run("image", "inspect", image, timeout=10)
    return rc == 0


def network_exists(network: str) -> bool:
    """Check if a Docker network exists."""
    _, rc = docker_run("network", "inspect", network, timeout=10)
    return rc == 0


def get_user_args() -> list[str]:
    """
    Get --user flags for docker run.

    Rootful: pin the container to the host UID/GID so bind-mounted files are
    host-user-owned. Rootless: pin to 0:0 — rootless maps container-root to the
    host user, so this both writes mounts correctly AND overrides any non-root
    USER baked into an image (e.g. the Telegram sidecar), which would otherwise
    map to an unprivileged subuid that cannot write host-owned mounts. The agent
    base image declares no USER, so 0:0 matches its existing rootless behavior.
    """
    if is_rootless():
        return ["--user", "0:0"]
    return ["--user", f"{os.getuid()}:{os.getgid()}"]


def get_tty_args() -> list[str]:
    """
    Return docker stdin/tty flags.

    Allocate a pseudo-TTY (-t) only when our own stdin is a terminal; Docker
    rejects -t when stdin is not a TTY (e.g. launched from a subprocess with
    stdin=DEVNULL or a pipe). Always pass -i so piped stdin still reaches the
    container.
    """
    if sys.stdin.isatty():
        return ["-it"]
    return ["-i"]
