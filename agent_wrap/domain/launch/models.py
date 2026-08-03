# This file has been created with the assistance of an AI tool.
"""Data models for the launch domain."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from typing import TextIO

    from agent_wrap.domain.sidecars.base import Sidecar


class DockerfileDirectives(NamedTuple):
    """Parsed directives from a Dockerfile.agent."""

    agent_user: str
    port_args: list[str]
    extra_run_args: list[str]


class HostNetworkResult(NamedTuple):
    """Resolved host-network configuration."""

    use_host_net: bool
    host_net_args: list[str]
    port_args: list[str]


class SidecarAssembly(NamedTuple):
    """Assembled sidecars with their secrets and Telegram availability."""

    sidecars: list[Sidecar]
    per_sidecar_secrets: dict[Sidecar, dict[str, str]]
    telegram_available: bool


class LaunchPreparation(NamedTuple):
    """Result of the under-lock prepare phase: agent flags plus held registrations."""

    #: Connectivity + env flags every ensured sidecar contributed.
    run_args: list[str]
    #: Held ``running/`` registration handles, keyed by **sidecar container name** (the
    #: refcount identity). Empty when a sidecar failed to ensure, since registration is
    #: all-or-nothing — the last action under the lock.
    running_handles: dict[str, TextIO | None]
