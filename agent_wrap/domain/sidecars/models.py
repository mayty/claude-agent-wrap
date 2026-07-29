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
