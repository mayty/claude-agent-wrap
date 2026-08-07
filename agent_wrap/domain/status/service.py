# This file has been created with the assistance of an AI tool.
"""
System-status aggregation domain service — the body of ``agent inspect``.

This service owns no state of its own. It composes the read-only accessors of the
domains that do own it (sidecars, secrets, logs, updates, config, providers) into one
``InspectReport``. Two properties are load-bearing:

**Read-only.** Nothing here writes, deletes, prompts, or reaches the network. Several
neighbouring methods that look like the obvious choice are unusable for exactly that
reason, and the alternatives exist because of it: ``SecretsService.present_keys`` instead
of ``check_secrets`` (which runs the legacy-keyfile migration), ``LogsService
.viewer_state`` instead of ``running_server`` (which unlinks a stale state file),
``SidecarService.registry_state`` instead of ``has_live_runners`` (which reaps stale
lock files), and ``UpdateService.current_revision`` instead of ``check_updates`` (which
fetches, and prompts).

**Total.** Every section degrades on its own. Docker being down empties the container
lists and leaves everything filesystem-derived intact; a section that cannot be read
reports absence rather than raising, because a diagnostic command is at its most useful
precisely when something is broken.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    BASE_IMAGE_NAME,
    DAY_START_HOURS,
    LITELLM_LOGS_DIRNAME,
    SIDECAR_NETWORK_NAME,
    TOOL_DIR,
)
from agent_wrap.domain.status.constants import (
    DAY_START_ENV,
    DOCKER_UNREACHABLE,
    HOST_NETWORK_ENV,
    TIMEZONE_ENV,
)
from agent_wrap.domain.status.models import (
    AgentRow,
    DockerStatus,
    EnvironmentRow,
    InspectReport,
    ProviderRow,
    SidecarRow,
    StorageRow,
    ViewerRow,
    WrapperRow,
)
from agent_wrap.lib import docker_utils
from agent_wrap.lib.utils import directory_size, is_truthy_env

if TYPE_CHECKING:
    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.logs.service import LogsService
    from agent_wrap.domain.providers.service import ProviderService
    from agent_wrap.domain.secrets.service import SecretsService
    from agent_wrap.domain.sidecars.service import SidecarService
    from agent_wrap.domain.updates.service import UpdateService


class InspectService:
    """Assembles a read-only snapshot of agent-wrap's state across the host."""

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        sidecar_service: SidecarService,
        provider_service: ProviderService,
        secrets_service: SecretsService,
        logs_service: LogsService,
        updates_service: UpdateService,
        config_service: ConfigService,
    ) -> None:
        self._sidecars = sidecar_service
        self._providers = provider_service
        self._secrets = secrets_service
        self._logs = logs_service
        self._updates = updates_service
        self._config = config_service

    def build_report(self) -> InspectReport:
        """
        Collect every section into one report.

        Docker is probed once up front so that "no containers" and "no daemon" stay
        distinguishable — both produce empty listings otherwise, and only one of them is
        a problem worth reporting.
        """
        docker_up = docker_utils.daemon_reachable()
        docker = DockerStatus(available=docker_up, error="" if docker_up else DOCKER_UNREACHABLE)

        registry = self._sidecars.registry_state(TOOL_DIR)
        sidecars = self._sidecar_rows(registry.by_container) if docker_up else []
        agents = self._agent_rows() if docker_up else []

        return InspectReport(
            docker=docker,
            sidecars=sidecars,
            agents=agents,
            queued_launches=registry.waiting,
            viewer=self._viewer_row(),
            providers=self._provider_rows(),
            wrapper=self._wrapper_row(),
            environment=self._environment_row(docker_up=docker_up),
            storage=self._storage_row(),
        )

    # --- per-section collectors ---

    def _sidecar_rows(self, by_container: dict[str, list[str]]) -> list[SidecarRow]:
        """Discover sidecar containers and fold in their live-agent counts."""
        return [
            SidecarRow(
                name=found.name,
                role=found.role,
                provider=found.provider,
                status=found.status,
                health=found.health,
                uptime_sec=found.uptime_sec,
                port=found.port,
                exit_code=found.exit_code,
                image=found.image,
                stale_image=found.stale_image,
                networks=found.networks,
                attached_agents=len(by_container.get(found.name, [])),
            )
            for found in self._sidecars.list_sidecar_containers()
        ]

    def _agent_rows(self) -> list[AgentRow]:
        """Discover agent containers, already annotated and ordered by image then cwd."""
        return [
            AgentRow(
                name=found.name,
                instance_id=found.instance_id,
                status=found.status,
                uptime_sec=found.uptime_sec,
                cwd=found.cwd,
                image=found.image,
                provider=found.provider,
                sidecars=found.sidecars,
            )
            for found in self._sidecars.list_agent_containers(TOOL_DIR)
        ]

    def _viewer_row(self) -> ViewerRow:
        """Read the logs viewer's state, adding its connect line when it is up."""
        state = self._logs.viewer_state()
        connect_line = (
            self._logs.connect_line(state.port) if state.running and state.port is not None else ""
        )
        return ViewerRow(
            running=state.running,
            pid=state.pid,
            port=state.port,
            connect_line=connect_line,
            log_size=state.log_size,
            log_mtime=state.log_mtime,
        )

    def _provider_rows(self) -> list[ProviderRow]:
        """
        Report every known sidecar's secret readiness, plus which provider is default.

        The default is resolved the same way ``agent run`` resolves it, so this answers
        "what would launch right now" rather than "what is installed". Telegram is
        included — it is a known secrets holder — but is never the default, since it is
        not a provider.
        """
        default_name = self._default_provider_name()
        return [
            ProviderRow(
                name=name,
                is_default=name == default_name,
                secrets_ok=not missing,
                missing_keys=missing,
            )
            for name, missing in sorted(self._secrets.missing_keys_by_sidecar().items())
        ]

    def _default_provider_name(self) -> str:
        """Name of the provider ``agent run`` would use, or "" if it cannot resolve."""
        try:
            return self._providers.get_provider().name
        except Exception:  # noqa: BLE001
            # An unresolvable provider (bad AGENT_PROVIDER, broken plugin) must not
            # abort the report — the provider list itself still shows what exists, and
            # the missing default is visible by its absence.
            return ""

    def _wrapper_row(self) -> WrapperRow:
        """Resolve the installed wrapper's git identity, locally."""
        revision = self._updates.current_revision()
        return WrapperRow(
            branch=revision.branch,
            commit=revision.commit,
            describe=revision.describe,
            dirty=revision.dirty,
        )

    def _environment_row(self, *, docker_up: bool) -> EnvironmentRow:
        """
        Collect the host facts behind the most common launch surprises.

        ``AGENT_USE_HOST_NETWORK`` is reported as requested-vs-effective because it is
        silently ignored off WSL, which otherwise looks like the setting not working.
        """
        requested = is_truthy_env(os.environ.get(HOST_NETWORK_ENV, ""))
        day_start_overridden = bool(os.environ.get(DAY_START_ENV))
        day_start_timezone = None if day_start_overridden else os.environ.get(TIMEZONE_ENV) or None
        image_present = docker_up and docker_utils.image_exists(BASE_IMAGE_NAME)
        version: str | None = None
        latest_version: str | None = None
        if image_present:
            version = docker_utils.image_claude_version(BASE_IMAGE_NAME)
            latest_version = docker_utils.latest_claude_version(BASE_IMAGE_NAME)
        return EnvironmentRow(
            base_image=BASE_IMAGE_NAME,
            base_image_present=image_present,
            base_image_version=version,
            latest_claude_version=latest_version,
            claude_update_available=docker_utils.is_newer_version(version, latest_version),
            network_name=SIDECAR_NETWORK_NAME,
            network_present=docker_up and docker_utils.network_exists(SIDECAR_NETWORK_NAME),
            host_network_requested=requested,
            host_network_effective=requested and docker_utils.is_wsl(),
            day_start_hours=DAY_START_HOURS,
            day_start_overridden=day_start_overridden,
            day_start_timezone=day_start_timezone,
        )

    def _storage_row(self) -> StorageRow:
        """Measure the shared logs tree and count registry entries."""
        registered = self._config.read_project_paths()
        return StorageRow(
            logs_bytes=directory_size(TOOL_DIR / LITELLM_LOGS_DIRNAME),
            projects_registered=len(registered),
            projects_stale=len(self._config.stale_project_paths()),
        )
