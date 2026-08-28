# This file has been created with the assistance of an AI tool.
"""
Data models for the system-status report.

Every class here is frozen and holds only scalars, lists of scalars, or other models
from this module. That is a hard requirement, twice over:

* the report is serialised with ``dataclasses.asdict`` + ``json.dumps`` for ``--json``,
  so a stray ``Path`` or ``datetime`` anywhere in the tree breaks that with no type
  error to warn about;
* the sidecar rows this composes are built from container environments full of live
  credentials, and a scalar-only shape leaves nowhere for one to hide.

Durations are stored as seconds and timestamps as epoch floats — formatting is the CLI's
concern, and a JSON consumer wants the number, not "3h 12m".
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DockerStatus:
    """Whether the Docker daemon answered, and why not if it didn't."""

    available: bool
    #: Human-readable reason when *available* is False; "" otherwise.
    error: str = ""


@dataclass(frozen=True)
class SidecarRow:
    """One sidecar container, with its live-agent count folded in."""

    name: str
    role: str
    provider: str
    status: str
    health: str
    uptime_sec: int | None
    port: int | None
    exit_code: int | None
    image: str
    stale_image: bool
    networks: list[str]
    #: Live agents holding a registration on this container. Zero is legitimate and
    #: transient — teardown drops registrations before it stops the container.
    attached_agents: int


@dataclass(frozen=True)
class AgentRow:
    """One agent container."""

    name: str
    instance_id: str
    status: str
    uptime_sec: int | None
    cwd: str
    #: Image the container runs — "claude-agent" or a per-project "claude-agent-<name>".
    image: str
    #: Provider this agent's traffic goes through; "" when it holds no registration.
    provider: str
    #: Sidecar container names this agent is registered on. Reported for machine
    #: consumers; the human table shows the *provider* derived from it instead, since the
    #: container names are already listed in the sidecar table above it.
    sidecars: list[str]


@dataclass(frozen=True)
class ViewerRow:
    """The logs viewer's state, plus its connect line when it is up."""

    running: bool
    pid: int | None
    port: int | None
    #: True when the process is alive but not listening yet, so *running* is already True
    #: while the connect line would point at nothing.
    starting: bool
    #: The viewer's own connect line, verbatim from the logs domain, or "" when it is not
    #: yet listening. Taken whole rather than reassembled from *port* so the two can
    #: never disagree.
    connect_line: str
    log_size: int | None
    log_mtime: float | None


@dataclass(frozen=True)
class AutostartRow:
    """
    Whether `agent run` would start the logs viewer, and what decides that.

    Kept apart from :class:`ViewerRow`, which reports the viewer process's own state: this
    is launch policy, read from the environment and the default provider rather than from
    anything running.

    Invariant the renderer relies on: *effective* is False only when *requested* is False
    or *declining_provider* is non-empty, so there is always a reason to name.
    """

    #: Tri-state AGENT_AUTOSTART_LOGS: None when unset, else its truthiness. The autostart
    #: is on by default, so "unset" and "explicitly off" are different answers here.
    requested: bool | None
    #: Whether the next non-headless `agent run` would actually start the viewer. Headless
    #: is a property of one launch's arguments, so it cannot be answered by a report.
    effective: bool
    #: The default provider's name when that provider is what turns the autostart off,
    #: else "" -- including when the provider could not be resolved at all.
    declining_provider: str


@dataclass(frozen=True)
class ProviderRow:
    """One known sidecar/provider and whether its secrets are all stored."""

    name: str
    #: Whether this is the provider `agent run` would use right now.
    is_default: bool
    #: True when every required secret is present. A provider requiring none is
    #: trivially satisfied.
    secrets_ok: bool
    #: Namespaced keys that are required but absent.
    missing_keys: list[str]


@dataclass(frozen=True)
class WrapperRow:
    """The installed wrapper's git identity, resolved without any network call."""

    branch: str
    commit: str
    describe: str
    dirty: bool


@dataclass(frozen=True)
class EnvironmentRow:
    """Host-level facts that explain most launch surprises."""

    base_image: str
    base_image_present: bool
    #: Version of the Claude Code CLI inside the base image (e.g. "2.0.50"),
    #: or None when the image is absent or the version could not be read.
    base_image_version: str | None
    #: Latest version of the Claude Code CLI on the npm registry (e.g. "2.0.51"),
    #: or None when it could not be checked (no network, registry unreachable).
    latest_claude_version: str | None
    #: True when the npm registry reports a newer version than the base image's.
    claude_update_available: bool
    network_name: str
    network_present: bool
    #: Whether AGENT_USE_HOST_NETWORK is set to a truthy value.
    host_network_requested: bool
    #: Whether it will actually take effect — it is honored only on WSL.
    host_network_effective: bool
    #: Hours past UTC midnight a stats "day" begins.
    day_start_hours: int
    #: Whether that came from AGENT_DAY_START_UTC rather than the host's local offset.
    day_start_overridden: bool
    #: AGENT_TIMEZONE's value when it's the effective source of day_start_hours (i.e.
    #: AGENT_DAY_START_UTC is unset); None otherwise, including when it's set but
    #: shadowed by AGENT_DAY_START_UTC.
    day_start_timezone: str | None


@dataclass(frozen=True)
class StorageRow:
    """On-disk footprint and project-registry counts."""

    #: Total size of the shared logs tree, or None when it was not measured -- lite mode
    #: skips the recursive walk. None is not zero, and the renderer says so.
    logs_bytes: int | None
    projects_registered: int
    projects_stale: int


@dataclass(frozen=True)
class ProjectImageRow:
    """The per-project image the cwd's Dockerfile declares, when it declares one."""

    #: Tag ``agent run`` would launch, e.g. "claude-agent-agent-wrap".
    image: str
    #: The project Dockerfile it is built from. A string, not a Path -- see the module
    #: docstring: anything unserialisable here breaks ``--json`` with no type error.
    dockerfile: str
    #: True when that Dockerfile still sits at the deprecated ``Dockerfile.agent`` path.
    is_legacy: bool
    present: bool
    #: Claude Code version inside it, or None when the image is absent or unreadable.
    claude_version: str | None
    #: True when the npm registry reports a newer version than this image's. Always False
    #: in lite mode, which never consults the registry.
    claude_update_available: bool


@dataclass(frozen=True)
class InspectReport:
    """The whole report — one screen of state, or one JSON document."""

    docker: DockerStatus
    sidecars: list[SidecarRow]
    agents: list[AgentRow]
    #: Instance ids queued for the shared sidecar lock (launches mid-start).
    queued_launches: list[str]
    viewer: ViewerRow
    #: Whether the next `agent run` would start that viewer, and what decides it.
    logs_autostart: AutostartRow
    providers: list[ProviderRow]
    wrapper: WrapperRow
    environment: EnvironmentRow
    storage: StorageRow
    #: The image this project's Dockerfile declares, or None when it declares none. None
    #: is the whole answer to "does this project customize its image" -- there is no
    #: empty-string stand-in to misread.
    project: ProjectImageRow | None = None
    #: Whether this report was collected in lite mode, which skips the npm-registry
    #: version check and the logs-size walk. Carried so a consumer can tell a value that
    #: was not measured from one measured as absent.
    lite: bool = False
    #: Non-fatal problems encountered while collecting, e.g. a stale registry entry.
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ImagePresence:
    """
    Which images exist locally -- the gate the version probes wait on, and nothing else.

    Separate from the rest because it is the report's one ordering constraint: reading a
    version starts a container from the image, and ``docker run`` on an image that is not
    there tries to *pull* it. Network presence is deliberately not part of this: it gates
    nothing, and folding it in would make the version probes wait on an unrelated call.
    """

    base: bool
    #: False when there is no project image to look for, as well as when it is absent.
    project: bool


@dataclass(frozen=True)
class ClaudeVersions:
    """
    Claude Code versions read in one parallel batch. None everywhere means "not read".

    Internal to the collection phase -- unlike its neighbours this never reaches
    ``InspectReport``; the rows built from it do.
    """

    base: str | None
    project: str | None
    #: Latest on the npm registry. Always None in lite mode, which never asks.
    latest: str | None
