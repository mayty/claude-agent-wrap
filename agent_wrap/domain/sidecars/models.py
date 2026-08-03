# This file has been created with the assistance of an AI tool.
"""Data models for the sidecars domain."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass(frozen=True)
class LiteLLMSidecarConfig:
    """Immutable configuration for a ``LiteLLMSidecar``, built by the provider."""

    # --- identity ---
    image: str
    container_name: str
    network_name: str
    internal_port: int
    master_key_prefix: str
    #: Provider name, passed to the sidecar as AGENT_WRAP_PROVIDER for log routing.
    provider_name: str

    # --- health / concurrency timing ---
    health_timeout_sec: int
    health_endpoint: str
    cold_start_time: float
    short_circuit_time: float

    # --- resolved paths (provider resolves these; introspecting the subclass) ---
    config_path: Path
    callback_dir: Path
    log_dir: Path

    # --- behavior hooks (provider-specific) ---
    get_sidecar_env: Callable[[dict[str, str]], dict[str, str]]
    get_agent_env: Callable[[str, str], dict[str, str]]
    on_started: Callable[[str], None]
    on_stopping: Callable[[str], None]

    # --- secrets ---
    #: Keys this sidecar requires from the secrets store.
    #: Each entry is ``(key_name, user_facing_description)``. The resolved values
    #: reach ``get_sidecar_env`` in a dict keyed by exactly these names, so a
    #: provider may declare none or several.
    required_secrets: list[tuple[str, str]]


@dataclass(frozen=True)
class TelegramSidecarConfig:
    """Immutable configuration for a ``TelegramSidecar``."""

    # --- identity ---
    image: str
    container_name: str
    network_name: str
    internal_port: int

    # --- per-run identity (for /register and /unregister on the sidecar) ---
    agent_name: str
    instance_id: str

    # --- health / concurrency timing ---
    health_timeout_sec: int
    #: Seconds a cold start takes (docker run + health poll).
    cold_start_time: float
    #: Seconds one agent takes to walk the lock on the hot path.
    short_circuit_time: float

    # --- paths ---
    log_dir: Path

    # --- headless ---
    #: When true, Claude Code runs in a mode that never exercises the sidecar
    #: (--bare/--safe-mode disable hooks; -p/--print is non-interactive). The
    #: sidecar is still *declared* so last-light-out teardown reaps the shared
    #: container, but its startup (prepare/ensure) is skipped.
    headless: bool = False


@dataclass(frozen=True)
class SidecarContainer:
    """
    One discovered sidecar container, as a reporting snapshot.

    Every field is a scalar, deliberately: the raw ``.Config.Env`` this is built from
    carries ``LITELLM_MASTER_KEY``, the upstream provider credential, and
    ``TELEGRAM_BOT_TOKEN``. There is no field here a secret could be stored in, so it
    cannot reach a report, a ``--json`` dump, or a traceback.
    """

    name: str
    #: "litellm", "telegram", or "unknown" — derived from the container name.
    role: str
    #: Provider this sidecar serves, or "" for the Telegram sidecar.
    provider: str
    #: Docker's own state string ("running", "exited", "created", …).
    status: str
    #: Health-check verdict, or "none" when the container declares no health check
    #: (the Telegram sidecar does not).
    health: str
    #: Seconds since the container started; None when it never started.
    uptime_sec: int | None
    #: Resolved listening port, or None when it could not be recovered.
    port: int | None
    #: Exit code, only meaningful when *status* is "exited".
    exit_code: int | None
    #: Image the container actually runs, for comparison against the pinned digest.
    image: str
    #: Whether *image* differs from the pin agent-wrap would start today.
    stale_image: bool
    #: Docker networks the container is attached to (names only, never IPs).
    networks: list[str]


@dataclass(frozen=True)
class AgentContainer:
    """
    One discovered agent container, as a reporting snapshot.

    Scalar-only for the same reason as :class:`SidecarContainer`: the ``.Mounts`` array
    this is built from lists every bind-mounted host path, of which only the project
    directory is wanted.
    """

    name: str
    #: The ``agent-wrap.instance-id`` label — the key the flock registry uses.
    instance_id: str
    #: Docker's own state string ("running", "exited", …).
    status: str
    #: Seconds since the container started; None when it never started.
    uptime_sec: int | None
    #: Host path mounted at /workspace, or "" when not recoverable.
    cwd: str
    #: Image the container runs — "claude-agent" or a per-project "claude-agent-<name>".
    image: str
    #: Provider this agent's model traffic goes through, derived from *sidecars*; "" when
    #: it holds no LiteLLM registration (a headless run, or one already tearing down).
    provider: str
    #: Sidecar container names this agent holds a live registration on.
    sidecars: list[str]


@dataclass(frozen=True)
class RegistryState:
    """
    The flock registry's live contents, read without mutating it.

    Assembled from ``running/<container>/<instance_id>`` and ``start-waiters/``. Note
    that an empty entry for a running container is a legitimate transient state, not an
    orphan: teardown clears registrations *before* taking the lock to stop the
    container (see ``LaunchService._release_sidecars``).
    """

    #: container name → live instance ids registered on it.
    by_container: dict[str, list[str]]
    #: instance ids queued for the shared sidecar lock.
    waiting: list[str]
