# This file has been created with the assistance of an AI tool.
"""
Sidecar management domain service.

The ONLY public API for the sidecar subpackage: every other domain subpackage goes
through an injected ``SidecarService``, never by importing ``base``, ``litellm``,
``telegram`` or ``tracker`` directly.
"""

import operator
from typing import TYPE_CHECKING, Any

from agent_wrap.constants import (
    CONTAINER_NAME_PREFIX,
    ROLE_LABEL,
    ROLE_VALUE,
    RUNNING_STATUS,
)
from agent_wrap.domain.sidecars.constants import (
    AGENT_INSPECT_TEMPLATE,
    SIDECAR_INSPECT_TEMPLATE,
)
from agent_wrap.domain.sidecars.discovery import ContainerRows
from agent_wrap.domain.sidecars.litellm import LiteLLMSidecar
from agent_wrap.domain.sidecars.models import (
    LiteLLMSidecarConfig,
    LiveContainers,
    RegistryState,
    TelegramSidecarConfig,
)
from agent_wrap.domain.sidecars.telegram import TelegramSidecar
from agent_wrap.domain.sidecars.tracker import SidecarTracker
from agent_wrap.lib.docker_utils import (
    docker_server_version,
    inspect_containers,
    list_container_names,
)
from agent_wrap.lib.flock import live_lock_ids

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.sidecars.models import (
        AgentContainer,
        SidecarContainer,
    )


class SidecarService:
    """Factory and coordinator for sidecar instances."""

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    def create_tracker(self, tool_dir: Path) -> SidecarTracker:
        return SidecarTracker(tool_dir)

    def create_telegram_sidecar(self, **kwargs: Any) -> TelegramSidecar:
        return TelegramSidecar(TelegramSidecarConfig(**kwargs), display_service=self._display)

    def create_litellm_sidecar(self, **kwargs: Any) -> LiteLLMSidecar:
        return LiteLLMSidecar(LiteLLMSidecarConfig(**kwargs), display_service=self._display)

    def telegram_required_secrets(self) -> list[tuple[str, str]]:
        return TelegramSidecar.required_secrets()

    def registry_state(self, tool_dir: Path) -> RegistryState:
        """
        Read the whole flock registry under *tool_dir* without mutating it.

        Reporting only, and mutates nothing -- a reader must not reap another run's
        state. The launch path asks the narrower ``SidecarTracker.has_live_runners``,
        which does reap as it goes.

        A container whose registration directory exists but holds no live entry is
        reported with an empty list rather than omitted: "known container, nobody
        attached" is worth telling apart from "never seen".
        """
        tracker = SidecarTracker(tool_dir)
        by_container: dict[str, list[str]] = {}
        if tracker.running_dir.is_dir():
            for entry in sorted(tracker.running_dir.iterdir()):
                # Files directly under running/ predate the per-container layout.
                if entry.is_dir():
                    by_container[entry.name] = live_lock_ids(entry)
        return RegistryState(
            by_container=by_container, waiting=live_lock_ids(tracker.start_waiters_dir)
        )

    def list_sidecar_containers(self) -> list[SidecarContainer]:
        """
        Discover every sidecar container on the host, running or not.

        Selected by the ``agent-wrap-`` name prefix: sidecar containers carry no
        agent-wrap labels, so the name is the only marker -- and the prefix also finds one
        left behind by a provider since removed from the install.

        Stopped containers are included on purpose: the Telegram sidecar runs without
        ``--rm`` so a crash during startup leaves its logs inspectable.
        """
        names = list_container_names(f"name=^{CONTAINER_NAME_PREFIX}-")
        lines, _rc = inspect_containers(names, SIDECAR_INSPECT_TEMPLATE)
        rows = [ContainerRows.sidecar(line) for line in lines]
        return sorted((row for row in rows if row is not None), key=operator.attrgetter("name"))

    def list_agent_containers(self, tool_dir: Path) -> list[AgentContainer]:
        """
        Discover every agent container, annotated with the sidecars it is attached to.

        The attachment is not a Docker fact -- no label links an agent to its sidecars --
        so it is inverted out of the flock registry under *tool_dir*.

        Ordered by image then project directory; sorting by container name would order by
        instance id, i.e. randomly.
        """
        state = self.registry_state(tool_dir)
        sidecars_by_instance: dict[str, list[str]] = {}
        for container, instance_ids in state.by_container.items():
            for instance_id in instance_ids:
                sidecars_by_instance.setdefault(instance_id, []).append(container)
        for containers in sidecars_by_instance.values():
            containers.sort()

        names = list_container_names(f"label={ROLE_LABEL}={ROLE_VALUE}")
        lines, _rc = inspect_containers(names, AGENT_INSPECT_TEMPLATE)
        rows = [ContainerRows.agent(line, sidecars_by_instance) for line in lines]
        return sorted(
            (row for row in rows if row is not None),
            key=operator.attrgetter("image", "cwd", "name"),
        )

    def live_containers(self, tool_dir: Path) -> LiveContainers:
        """
        Every agent container and sidecar Docker currently reports as running.

        Gated on ``docker_server_version`` first: ``list_container_names`` returns [] both for
        "nothing matched" and "docker is unavailable", and a caller that refuses to act
        while something is live must not confuse the two. An unreachable daemon then
        reads as nothing running -- a host whose Docker is down has no agent to protect,
        and treating it as live would leave the wrapper unable to update itself there.

        Asks Docker rather than the flock registry, so it also sees an agent whose
        registration is already cleared while its container is still shutting down.
        """
        if docker_server_version() is None:
            return LiveContainers(agents=[], sidecars=[])
        return LiveContainers(
            agents=[c for c in self.list_agent_containers(tool_dir) if c.status == RUNNING_STATUS],
            sidecars=[c for c in self.list_sidecar_containers() if c.status == RUNNING_STATUS],
        )
