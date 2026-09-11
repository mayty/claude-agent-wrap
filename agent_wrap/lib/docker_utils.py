# This file has been edited with the assistance of an AI tool.
"""Docker-related utility functions."""

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

    Cached for the process lifetime, so a test patching ``docker_run`` must call
    ``is_rootless.cache_clear()`` or the value leaks between cases.
    """
    stdout, _ = docker_run("info", timeout=10)
    return "rootless" in stdout.lower()


def daemon_reachable() -> bool:
    """
    Report whether the Docker daemon answers at all.

    Tells "no containers match" apart from "docker is down", which look identical in a
    listing's empty output.
    """
    _, rc = docker_run("version", "--format", "{{.Server.Version}}", timeout=10)
    return rc == 0


def list_container_names(*filters: str) -> list[str]:
    """
    List names of containers matching every ``docker ps --filter`` expression given.

    Includes stopped containers (``-a``). Returns [] both when nothing matches and when
    docker is unavailable -- indistinguishable on purpose, so callers that care about
    the difference use :func:`daemon_reachable`.
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

    The rc is returned rather than interpreted: a non-zero rc *with* output is routine,
    since a container that vanished between listing and inspection makes docker report an
    error for that name while still printing rows for the others.
    """
    if not names:
        return [], 0
    stdout, rc = docker_run("container", "inspect", "--format", template, *names)
    return [line for line in stdout.splitlines() if line.strip()], rc


def list_images(
    *filters: str, template: str, reference: str = "", digests: bool = False
) -> list[str]:
    """
    List local images matching every ``docker image ls --filter`` expression given.

    *template* must keep each image on a single line or the caller's field split breaks.
    *reference* narrows the listing to one repository.

    *digests* passes ``--digests``, and a *template* naming ``{{.Digest}}`` must set it:
    the flag is what populates that field, so without it docker renders ``<none>`` for
    every row rather than failing on the template.

    Returns [] both when nothing matches and when docker is unavailable, as
    :func:`list_container_names` does.
    """
    args = ["image", "ls", "--format", template]
    if digests:
        args.append("--digests")
    for expr in filters:
        args.extend(["--filter", expr])
    if reference:
        args.append(reference)
    stdout, rc = docker_run(*args)
    if rc != 0:
        return []
    return [line for line in stdout.splitlines() if line.strip()]


def inspect_images(names: list[str], template: str) -> list[str]:
    """
    Batch-inspect image *names* with a Go *template*, returning its output lines.

    ``image inspect`` for the same reason :func:`inspect_containers` uses ``container
    inspect``: plain ``inspect`` matches either kind. *template* must render each image
    on a single line.

    Unlike :func:`inspect_containers` the rc is dropped: an image that vanished between
    the two calls is a row to leave alone rather than a failure to report.
    """
    if not names:
        return []
    stdout, _ = docker_run("image", "inspect", "--format", template, *names)
    return [line for line in stdout.splitlines() if line.strip()]


def remove_image(ref: str) -> bool:
    """
    Remove the image *ref* names, reporting whether docker did it.

    Deliberately without ``--force``: docker refuses to remove an image a container still
    references, and that refusal is the safety net a caller wants reported rather than
    overridden.
    """
    _, rc = docker_run("rmi", ref, timeout=60)
    return rc == 0


class ImageRef(NamedTuple):
    """
    The three parts of an image reference, any of which may be absent ("").

    ``repository`` keeps any registry host and namespace ("ghcr.io/berriai/litellm"), so
    it compares directly against what ``docker image ls`` renders for ``{{.Repository}}``.
    """

    repository: str
    tag: str
    digest: str


def parse_image_ref(ref: str) -> ImageRef:
    """
    Split an image reference into repository, tag and digest.

    The tag is separated on the *last* colon, and only when no ``/`` follows it, so a
    registry port ("localhost:5000/img") is not mistaken for a tag.
    """
    remainder, _, digest = ref.partition("@")
    repository, sep, tag = remainder.rpartition(":")
    if not sep or "/" in tag:
        return ImageRef(repository=remainder, tag="", digest=digest)
    return ImageRef(repository=repository, tag=tag, digest=digest)


def parse_docker_timestamp(raw: str) -> datetime | None:
    """
    Parse a docker RFC3339 timestamp into a UTC-aware datetime, or None if unusable.

    ``fromisoformat`` handles every shape docker emits, so no normalizing is needed.

    Docker's zero timestamp (meaning "never") returns None, and so does a bare date:
    ``fromisoformat`` would read it as midnight, but docker never emits one, so it means
    the caller was handed something else.
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
    _, rc = docker_run("image", "inspect", image, timeout=10)
    return rc == 0


class ImageStamp(NamedTuple):
    """
    Identity and labels of a local image, read in a single inspect.

    ``labels`` carries what docker reports on ``Config.Labels``, which *includes* every
    label inherited through ``FROM`` -- a derived image cannot be told apart from its
    parent by a label the parent set. Callers that care must know which image class they
    are reading a given label off.
    """

    #: The image's content id, e.g. "sha256:...".
    id: str
    #: Labels, or {} when the image declares none.
    labels: dict[str, str]


def image_stamp(image: str) -> ImageStamp | None:
    """
    Id and labels of *image*, or None when it is absent or docker is unreachable.

    Doubles as the existence probe, so both facts cost one docker call. Labels come back
    as JSON rather than through ``{{index .Config.Labels "k"}}``, which renders an absent
    key and an empty value identically -- and absence is a state of its own here ("built
    before stamping").
    """
    stdout, rc = docker_run(
        "image", "inspect", "--format", "{{.Id}} {{json .Config.Labels}}", image, timeout=10
    )
    if rc != 0 or not stdout:
        return None
    image_id, _, raw_labels = stdout.partition(" ")
    try:
        parsed = json.loads(raw_labels)
    except json.JSONDecodeError:
        return ImageStamp(id=image_id, labels={})
    if not isinstance(parsed, dict):
        return ImageStamp(id=image_id, labels={})
    return ImageStamp(id=image_id, labels={str(k): str(v) for k, v in parsed.items()})


def image_claude_version(image: str) -> str | None:
    """
    Return the @anthropic-ai/claude-code version inside *image*, or None.

    A non-zero npm exit code is tolerated: ``npm ls`` flags dependency problems with rc=1
    while still printing the JSON with the version.
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

    Runs ``npm view`` in a short-lived container, so this one reaches the network -- and
    carries a longer timeout than :func:`image_claude_version` for that reason.
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

    Compared as integer tuples, so "2.0.10" sorts after "2.0.9". False when either side
    is None or unparseable -- an unknown latest version must never look like an update.
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
    _, rc = docker_run("network", "inspect", network, timeout=10)
    return rc == 0


def get_user_args() -> list[str]:
    """
    Get --user flags for docker run.

    Rootful pins the host UID/GID so bind-mounted files are host-user-owned. Rootless
    pins 0:0, which maps to the host user -- and also overrides any non-root USER baked
    into an image, which would otherwise map to a subuid that cannot write host mounts.
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

    ``-t`` only when our own stdin is a terminal: Docker rejects it otherwise. Always
    ``-i``, so piped stdin still reaches the container.
    """
    if sys.stdin.isatty():
        return ["-it"]
    return ["-i"]


class MountSpec(NamedTuple):
    """
    One mount declared on a ``docker run`` command line.

    ``source`` is the host side exactly as authored, and ``None`` for named and anonymous
    volumes. Left unresolved on purpose: only the caller knows which directory a relative
    path is resolved against.
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

    Unparseable specs are dropped rather than reported: docker is the authority on what
    it accepts, and this parser exists only to decide which host paths to pre-create.
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
