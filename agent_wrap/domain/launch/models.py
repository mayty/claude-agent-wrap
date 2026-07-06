# This file has been created with the assistance of an AI tool.
"""Data models for the launch domain."""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
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
