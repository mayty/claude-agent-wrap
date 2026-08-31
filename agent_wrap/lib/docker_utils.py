# This file has been edited with the assistance of an AI tool.
"""Docker-related utility functions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import NamedTuple

from agent_wrap.lib.utils import is_truthy_env

# What docker reports for a timestamp that never happened (e.g. StartedAt on a
# container that was created but never started). It parses fine and would yield a
# ~2000-year uptime, so it is mapped to None instead.
_ZERO_TIMESTAMP_YEAR = 1


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
    except subprocess.TimeoutExpired, FileNotFoundError, subprocess.SubprocessError:
        return "", 1
    stdout = result.stdout.strip() if result.stdout is not None else ""
    return stdout, result.returncode


@cache
def is_rootless() -> bool:
    """
    Check if Docker is running in rootless mode.

    Cached: this shells out to ``docker info`` (10 s timeout) and the answer is
    constant for the process lifetime. Tests that patch ``docker_run`` must call
    ``is_rootless.cache_clear()`` so no value leaks between cases.
    """
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

    ``fromisoformat`` handles every shape docker emits: a trailing ``Z``, a colon-less
    offset (``+0200``), and nanosecond precision (truncated to microseconds, which is
    ample for the uptimes this feeds). This used to be a hand-rolled regex normalizer
    because the supported floor was Python 3.10, which accepted none of the three.

    Docker's zero timestamp (``0001-01-01T00:00:00Z``, meaning "never") returns None,
    and so does a bare date: ``fromisoformat`` would happily read it as midnight, but
    docker never emits one, so it means the caller was handed something else.
    """
    text = raw.strip()
    if "T" not in text and " " not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.year <= _ZERO_TIMESTAMP_YEAR:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def image_exists(image: str) -> bool:
    """Check if a Docker image exists locally."""
    _, rc = docker_run("image", "inspect", image, timeout=10)
    return rc == 0


def image_claude_version(image: str) -> str | None:
    """
    Return the @anthropic-ai/claude-code version inside *image*, or None.

    Reads the installed global npm package via ``npm ls --json`` in a short-lived
    container. Returns None when the command times out or the version cannot be
    parsed. A non-zero npm exit code is tolerated: ``npm ls`` flags dependency
    problems with rc=1 while still printing the JSON with the version.
    """
    stdout, _ = docker_run(
        "run",
        "--rm",
        "--entrypoint",
        "",
        image,
        "npm",
        "ls",
        "@anthropic-ai/claude-code",
        "--global",
        "--depth=0",
        "--json",
        timeout=10,
    )
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
        package = data.get("dependencies", {}).get("@anthropic-ai/claude-code", {})
        return package.get("version")
    except json.JSONDecodeError, AttributeError:
        return None


def latest_claude_version(image: str) -> str | None:
    """
    Return the latest @anthropic-ai/claude-code version on the npm registry, or None.

    Runs ``npm view`` — which queries the registry over the network — in a
    short-lived container from *image*. Returns None when the command times out,
    the registry is unreachable, or the output cannot be parsed. The timeout is
    longer than :func:`image_claude_version`'s because this actually reaches the
    network.
    """
    stdout, _ = docker_run(
        "run",
        "--rm",
        "--entrypoint",
        "",
        image,
        "npm",
        "view",
        "@anthropic-ai/claude-code",
        "version",
        timeout=15,
    )
    line = stdout.strip().splitlines()[0].strip() if stdout else ""
    return line or None


def is_newer_version(installed: str | None, latest: str | None) -> bool:
    """
    Whether *latest* is a newer version than *installed*.

    Compares dot-separated numeric parts ("2.0.50" -> (2, 0, 50)) as tuples, so
    "2.0.10" sorts after "2.0.9". Returns False when either side is None or
    cannot be parsed — an unknown latest version must never look like an update.
    """
    if not installed or not latest:
        return False
    try:
        installed_parts = tuple(int(part) for part in installed.split("."))
        latest_parts = tuple(int(part) for part in latest.split("."))
    except ValueError:
        return False
    if not installed_parts or not latest_parts:
        return False
    return latest_parts > installed_parts


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


def get_container_uid() -> int:
    """
    UID the agent container actually runs as.

    Mirrors the branch in ``get_user_args`` so the two stay one decision. Needed
    because Claude Code derives its per-session temp dir from the effective UID
    (``/tmp/claude-<uid>``), which the wrapper has to bind-mount by exact path.
    """
    if is_rootless():
        return 0
    return os.getuid()


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


class MountSpec(NamedTuple):
    """
    One mount declared on a ``docker run`` command line.

    ``source`` is the host side exactly as it was authored, and only when the spec
    names a host path at all: named and anonymous volumes carry ``None``. It is left
    unresolved on purpose -- the caller knows which directory a relative path is
    resolved against, and nothing here rewrites what the author wrote.
    """

    source: str | None
    target: str
    read_only: bool


# How docker itself tells a host path from a volume name in a short-form spec: a
# volume name may not contain "/", so anything starting with one of these is a bind
# source. "~" is not in the list for docker (which rejects such a spec outright); it
# is recorded here so callers can point out that no shell is involved to expand it.
_HOST_PATH_PREFIXES = ("/", "./", "../", "~")

# Flag -> spec syntax. Every one of these also accepts a --flag=value form.
_MOUNT_FLAGS = {
    "-v": "short",
    "--volume": "short",
    "--mount": "mount",
    "--tmpfs": "tmpfs",
}


class _MountSpecParser:
    """Parsers for the individual ``docker run`` mount spec syntaxes."""

    @staticmethod
    def short_form(spec: str) -> MountSpec | None:
        """Parse a ``-v``/``--volume`` spec: ``[src:]dst[:opts]``."""
        source_text, _, remainder = spec.partition(":")
        if not remainder:
            # Anonymous volume -- container side only.
            return MountSpec(source=None, target=spec, read_only=False)
        target, _, opts_text = remainder.partition(":")
        if ":" in opts_text:
            return None  # more fields than src:dst:opts -- docker will reject it
        source = source_text if source_text.startswith(_HOST_PATH_PREFIXES) else None
        return MountSpec(source=source, target=target, read_only="ro" in opts_text.split(","))

    @staticmethod
    def mount_form(spec: str) -> MountSpec | None:
        """
        Parse a ``--mount`` spec: comma-separated ``key=value`` pairs.

        Splitting on "," naively mis-reads the nested comma syntax of
        ``volume-opt=o=addr=...``, which only ever appears on ``type=volume`` mounts --
        those contribute no host source, so the misread costs nothing.
        """
        fields: dict[str, str] = {}
        for field in spec.split(","):
            key, _, value = field.partition("=")
            fields[key.strip().lower()] = value.strip()

        target = fields.get("target") or fields.get("destination") or fields.get("dst")
        if not target:
            return None

        source = fields.get("source") or fields.get("src")
        if (
            fields.get("type", "volume") != "bind"
            or not source
            or not source.startswith(_HOST_PATH_PREFIXES)
        ):
            source = None

        raw_ro = fields.get("readonly", fields.get("ro"))
        read_only = raw_ro is not None and raw_ro.lower() not in ("false", "0")
        return MountSpec(source=source, target=target, read_only=read_only)


def parse_mount_specs(args: list[str]) -> list[MountSpec]:
    """
    Extract every mount declared in a list of ``docker run`` flags.

    Recognizes ``-v``/``--volume``, ``--mount`` and ``--tmpfs``, in both the
    ``--flag value`` and ``--flag=value`` spellings. Unparseable specs are dropped
    rather than reported: docker is the authority on what it accepts, and this parser
    exists only to decide which host paths to pre-create.
    """
    specs: list[MountSpec] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if kind := _MOUNT_FLAGS.get(arg):
            value = args[index + 1] if index + 1 < len(args) else ""
            index += 2
        else:
            name, sep, rest = arg.partition("=")
            kind = _MOUNT_FLAGS.get(name) if sep else None
            value = rest
            index += 1
        if not kind or not value:
            continue

        if kind == "short":
            spec = _MountSpecParser.short_form(value)
        elif kind == "mount":
            spec = _MountSpecParser.mount_form(value)
        else:
            spec = MountSpec(source=None, target=value, read_only=False)
        if spec is not None:
            specs.append(spec)
    return specs
