# This file has been created with the assistance of an AI tool.
"""
Sidecar management domain service.

This is the ONLY public API for the sidecar subpackage. Every other domain
subpackage accesses sidecar functionality through an injected
``SidecarService`` instance — never by importing the internal modules
(``base``, ``litellm``, ``telegram``, ``tracker``) directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_wrap.constants import CONTAINER_NAME_PREFIX
from agent_wrap.domain.sidecars.constants import (
    AGENT_INSPECT_TEMPLATE,
    ROLE_LABEL,
    ROLE_VALUE,
    SIDECAR_INSPECT_TEMPLATE,
)
from agent_wrap.domain.sidecars.discovery import ContainerRows
from agent_wrap.domain.sidecars.litellm import LiteLLMSidecar
from agent_wrap.domain.sidecars.models import LiteLLMSidecarConfig, TelegramSidecarConfig
from agent_wrap.domain.sidecars.telegram import TelegramSidecar
from agent_wrap.domain.sidecars.tracker import SidecarTracker
from agent_wrap.lib.docker_utils import inspect_containers, list_container_names

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.sidecars.models import (
        AgentContainer,
        RegistryState,
        SidecarContainer,
    )


class SidecarService:
    """
    Factory and coordinator for sidecar instances.

    Injected via constructor DI into every domain service that needs to
    create or manage sidecars.
    """

    #: Label value used to identify agent containers.
    role_label: str = ROLE_LABEL
    #: Label role value identifying agent containers.
    role_value: str = ROLE_VALUE

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    # --- Factory methods ---

    def create_tracker(self, tool_dir: Path) -> SidecarTracker:
        """Create a new ``SidecarTracker`` scoped to *tool_dir*."""
        return SidecarTracker(tool_dir)

    def create_telegram_sidecar(self, **kwargs: Any) -> TelegramSidecar:
        """Create a ``TelegramSidecar`` from keyword arguments forwarded to the config."""
        return TelegramSidecar(TelegramSidecarConfig(**kwargs), display_service=self._display)

    def create_litellm_sidecar(self, **kwargs: Any) -> LiteLLMSidecar:
        """Create a ``LiteLLMSidecar`` from keyword arguments forwarded to the config."""
        return LiteLLMSidecar(LiteLLMSidecarConfig(**kwargs), display_service=self._display)

    def telegram_required_secrets(self) -> list[tuple[str, str]]:
        """Return the secrets required by the Telegram sidecar."""
        return TelegramSidecar.required_secrets()

    # --- Discovery (read-only; for reporting, never for the launch decision) ---

    def registry_state(self, tool_dir: Path) -> RegistryState:
        """
        Read the whole flock registry under *tool_dir* without mutating it.

        Reporting only. The launch path asks its own narrower question through
        ``SidecarTracker.has_live_runners``, which reaps stale entries as it goes.
        """
        return SidecarTracker(tool_dir).registry_state()

    def list_sidecar_containers(self) -> list[SidecarContainer]:
        """
        Discover every sidecar container on the host, running or not.

        Selected by the ``agent-wrap-`` name prefix, which covers a provider's
        ``agent-wrap-<provider>`` and the single ``agent-wrap-telegram`` while excluding
        agent containers (``claude-agent-<instance_id>``). Sidecar containers carry no
        agent-wrap labels, so the name is the only marker available — and the prefix has
        the advantage of also finding a sidecar left behind by a provider that has since
        been removed from the install.

        Stopped containers are included on purpose: the Telegram sidecar runs without
        ``--rm`` so that a crash during startup leaves its logs inspectable, and that
        corpse is exactly what a report should surface.
        """
        names = list_container_names(f"name=^{CONTAINER_NAME_PREFIX}-")
        lines, _rc = inspect_containers(names, SIDECAR_INSPECT_TEMPLATE)
        rows = [ContainerRows.sidecar(line) for line in lines]
        return sorted((row for row in rows if row is not None), key=lambda row: row.name)

    def list_agent_containers(self, tool_dir: Path) -> list[AgentContainer]:
        """
        Discover every agent container, annotated with the sidecars it is attached to.

        The attachment is not a Docker fact — no label links an agent to its sidecars —
        so it is inverted out of the flock registry under *tool_dir*, whose layout is
        ``running/<container_name>/<instance_id>``.

        Ordered by image then project directory, which groups a fleet the way its owner
        thinks about it. Sorting by container name instead would order by instance id,
        i.e. randomly. The name breaks remaining ties so the order stays stable.
        """
        state = self.registry_state(tool_dir)
        sidecars_by_instance: dict[str, list[str]] = {}
        for container, instance_ids in state.by_container.items():
            for instance_id in instance_ids:
                sidecars_by_instance.setdefault(instance_id, []).append(container)
        for containers in sidecars_by_instance.values():
            containers.sort()

        names = list_container_names(f"label={self.role_label}={self.role_value}")
        lines, _rc = inspect_containers(names, AGENT_INSPECT_TEMPLATE)
        rows = [ContainerRows.agent(line, sidecars_by_instance) for line in lines]
        return sorted(
            (row for row in rows if row is not None),
            key=lambda row: (row.image, row.cwd, row.name),
        )
