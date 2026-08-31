# This file has been created with the assistance of an AI tool.
"""
System-status aggregation domain service — the body of ``agent inspect``.

This service owns no state of its own. It composes the read-only accessors of the
domains that do own it (sidecars, secrets, logs, updates, config, providers, build) into
one ``InspectReport``. Three properties are load-bearing:

**Read-only.** Nothing here writes, deletes, or prompts. Several neighbouring methods that
look like the obvious choice are unusable for exactly that reason, and the alternatives
exist because of it: ``SecretsService.present_keys`` instead of ``check_secrets`` (which
runs the legacy-keyfile migration), ``LogsService.viewer_state`` instead of
``running_server`` (which unlinks a stale state file), ``SidecarService.registry_state``
instead of ``has_live_runners`` (which reaps stale lock files), ``UpdateService
.current_revision`` instead of ``check_updates`` (which fetches, and prompts), and
``BuildService.resolve_image`` instead of anything further along the rebuild path (which
builds).

Read-only is not the same as cheap, and two probes are neither local nor silent: reading
the Claude Code version inside an image starts a throwaway container from it, and the "is
there a newer one" check runs ``npm view`` in that container, which reaches the npm
registry. ``lite=True`` drops the registry call and the recursive logs-size walk — the two
slowest things the report does — and keeps everything else, including both installed
versions.

**Total.** Every section degrades on its own. Docker being down empties the container
lists and leaves everything filesystem-derived intact; a section that cannot be read
reports absence rather than raising, because a diagnostic command is at its most useful
precisely when something is broken. A project Dockerfile that cannot be resolved becomes a
warning on the report rather than an abort.

**Concurrent.** The Docker probes fan out over a thread pool. They share no state, each
already degrades to None/False/[] on its own failure, and every one of them is a
``subprocess.run`` that spends its time waiting — so the pool buys wall clock and costs no
new failure mode. Completion order is not observable in the report. The single ordering
constraint is that a version probe must never run against an absent image, since
``docker run`` would then try to *pull* it; that, and only that, is why the presence
probes are an awaited phase of their own instead of another handful of futures.
"""

from __future__ import annotations

import os
import platform
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AUTOSTART_LOGS_ENV,
    BASE_IMAGE_NAME,
    DAY_START_HOURS,
    LITELLM_LOGS_DIRNAME,
    PYTHON_PIN_FILE,
    SIDECAR_NETWORK_NAME,
    TOOL_DIR,
)
from agent_wrap.domain.status.constants import (
    DAY_START_ENV,
    DOCKER_UNREACHABLE,
    HOST_NETWORK_ENV,
    PROBE_THREAD_PREFIX,
    PROBE_WORKERS,
    TIMEZONE_ENV,
)
from agent_wrap.domain.status.models import (
    AgentRow,
    AutostartRow,
    ClaudeVersions,
    DockerStatus,
    EnvironmentRow,
    ImagePresence,
    InspectReport,
    ProjectImageRow,
    ProviderRow,
    SidecarRow,
    StorageRow,
    ViewerRow,
    WrapperRow,
)
from agent_wrap.lib import docker_utils
from agent_wrap.lib.utils import directory_size, is_truthy_env, optional_truthy_env

if TYPE_CHECKING:
    from agent_wrap.domain.build.models import ResolvedImage
    from agent_wrap.domain.build.service import BuildService
    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.logs.service import LogsService
    from agent_wrap.domain.providers.base import Provider
    from agent_wrap.domain.providers.service import ProviderService
    from agent_wrap.domain.secrets.service import SecretsService
    from agent_wrap.domain.sidecars.models import AgentContainer, SidecarContainer
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
        build_service: BuildService,
    ) -> None:
        self._sidecars = sidecar_service
        self._providers = provider_service
        self._secrets = secrets_service
        self._logs = logs_service
        self._updates = updates_service
        self._config = config_service
        self._build = build_service

    def build_report(self, *, lite: bool = False) -> InspectReport:
        """
        Collect every section into one report.

        Docker is probed once up front so that "no containers" and "no daemon" stay
        distinguishable — both produce empty listings otherwise, and only one of them is
        a problem worth reporting. Everything that talks to Docker afterwards goes through
        the pool; the filesystem-derived sections stay sequential, being cheap enough that
        a thread would cost more than it saves.
        """
        docker_up = docker_utils.daemon_reachable()
        docker = DockerStatus(available=docker_up, error="" if docker_up else DOCKER_UNREACHABLE)

        registry = self._sidecars.registry_state(TOOL_DIR)
        resolved, project_warning = self._project_dockerfile()

        with ThreadPoolExecutor(
            max_workers=PROBE_WORKERS, thread_name_prefix=PROBE_THREAD_PREFIX
        ) as pool:
            # Submitted first and awaited last. None of these gates anything, so they run
            # underneath both probe phases below rather than adding to them.
            sidecars_future = (
                pool.submit(self._sidecars.list_sidecar_containers) if docker_up else None
            )
            agents_future = (
                pool.submit(self._sidecars.list_agent_containers, TOOL_DIR) if docker_up else None
            )
            network_future = (
                pool.submit(docker_utils.network_exists, SIDECAR_NETWORK_NAME)
                if docker_up
                else None
            )
            logs_future = (
                None if lite else pool.submit(directory_size, TOOL_DIR / LITELLM_LOGS_DIRNAME)
            )

            presence = self._probe_presence(pool, resolved, docker_up=docker_up)
            versions = self._probe_versions(pool, resolved, presence, lite=lite)

            network_present = bool(network_future and network_future.result())
            found_sidecars = sidecars_future.result() if sidecars_future else []
            found_agents = agents_future.result() if agents_future else []
            logs_bytes = logs_future.result() if logs_future else None

        return InspectReport(
            docker=docker,
            sidecars=self._sidecar_rows(found_sidecars, registry.by_container),
            agents=self._agent_rows(found_agents),
            queued_launches=registry.waiting,
            viewer=self._viewer_row(),
            logs_autostart=self._autostart_row(),
            providers=self._provider_rows(),
            wrapper=self._wrapper_row(),
            environment=self._environment_row(
                presence=presence, versions=versions, network_present=network_present
            ),
            storage=self._storage_row(logs_bytes=logs_bytes),
            project=self._project_image_row(resolved, presence=presence, versions=versions),
            lite=lite,
            warnings=[project_warning] if project_warning else [],
        )

    # --- docker probes, fanned out ---

    def _probe_presence(
        self, pool: ThreadPoolExecutor, resolved: ResolvedImage | None, *, docker_up: bool
    ) -> ImagePresence:
        """
        Ask Docker which of the two images exist, both at once.

        This is the report's only barrier, and it buys the one thing worth waiting for: a
        version probe starts a container from its image, and ``docker run`` against an
        image that is not present locally attempts a registry pull. Two concurrent
        ``docker image inspect`` calls are what that costs.
        """
        if not docker_up:
            return ImagePresence(base=False, project=False)
        base_future = pool.submit(docker_utils.image_exists, BASE_IMAGE_NAME)
        project_future = (
            pool.submit(docker_utils.image_exists, resolved.image) if resolved else None
        )
        return ImagePresence(
            base=base_future.result(),
            project=bool(project_future and project_future.result()),
        )

    def _probe_versions(
        self,
        pool: ThreadPoolExecutor,
        resolved: ResolvedImage | None,
        presence: ImagePresence,
        *,
        lite: bool,
    ) -> ClaudeVersions:
        """
        Read the installed Claude Code versions, and the registry's latest, in one batch.

        Each of these starts a short-lived container, so running them one after another is
        most of the report's wall clock. They go in together, each gated only on its own
        image. ``latest`` is the single registry lookup for the whole report — both images
        are compared against it — and lite mode omits it.
        """
        base_future = (
            pool.submit(docker_utils.image_claude_version, BASE_IMAGE_NAME)
            if presence.base
            else None
        )
        project_future = (
            pool.submit(docker_utils.image_claude_version, resolved.image)
            if resolved is not None and presence.project
            else None
        )
        latest_future = (
            pool.submit(docker_utils.latest_claude_version, BASE_IMAGE_NAME)
            if presence.base and not lite
            else None
        )
        return ClaudeVersions(
            base=base_future.result() if base_future else None,
            project=project_future.result() if project_future else None,
            latest=latest_future.result() if latest_future else None,
        )

    # --- per-section collectors ---

    def _project_dockerfile(self) -> tuple[ResolvedImage | None, str]:
        """
        Resolve the cwd's project image, or explain why it could not be resolved.

        Goes through ``BuildService.resolve_image`` rather than probing paths here: that is
        the wrapper's single Dockerfile discovery point, and a second implementation would
        be free to drift from the one ``agent run`` actually launches.

        Returns:
            The resolved project image and "", or None and a warning for the report. None
            with an empty warning means the project simply declares no Dockerfile —
            ``agent_name is None`` is that predicate, not the file's basename.

        """
        try:
            resolved = self._build.resolve_image()
        except SystemExit as exc:
            # Both Dockerfile locations populated, or a missing/invalid `# agent-name:`.
            # Fatal to a launch; here it costs one row, and the rest of the report is
            # exactly what someone diagnosing that state wants to see.
            return None, str(exc)
        except OSError as exc:
            return None, f"project Dockerfile could not be read: {exc}"
        return (resolved, "") if resolved.agent_name is not None else (None, "")

    def _sidecar_rows(
        self, found: list[SidecarContainer], by_container: dict[str, list[str]]
    ) -> list[SidecarRow]:
        """Fold the live-agent counts into the sidecar containers the pool discovered."""
        return [
            SidecarRow(
                name=container.name,
                role=container.role,
                provider=container.provider,
                status=container.status,
                health=container.health,
                uptime_sec=container.uptime_sec,
                port=container.port,
                exit_code=container.exit_code,
                image=container.image,
                stale_image=container.stale_image,
                networks=container.networks,
                attached_agents=len(by_container.get(container.name, [])),
            )
            for container in found
        ]

    def _agent_rows(self, found: list[AgentContainer]) -> list[AgentRow]:
        """Map the agent containers the pool discovered, already annotated and ordered."""
        return [
            AgentRow(
                name=container.name,
                instance_id=container.instance_id,
                status=container.status,
                uptime_sec=container.uptime_sec,
                cwd=container.cwd,
                image=container.image,
                provider=container.provider,
                sidecars=container.sidecars,
            )
            for container in found
        ]

    def _viewer_row(self) -> ViewerRow:
        """Read the logs viewer's state, adding its connect line when it is up."""
        state = self._logs.viewer_state()
        # No connect line for a starting viewer: its recorded port is the one that was
        # requested, and bind_port scans past it when it is taken.
        listening = state.running and not state.starting
        connect_line = (
            self._logs.connect_line(state.port) if listening and state.port is not None else ""
        )
        return ViewerRow(
            running=state.running,
            pid=state.pid,
            port=state.port,
            starting=state.starting,
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

    def _default_provider(self) -> Provider | None:
        """
        Resolve the provider ``agent run`` would use, or None when it cannot be resolved.

        An unresolvable provider (bad AGENT_PROVIDER, broken plugin) must not abort the
        report — the provider list itself still shows what exists, and the missing default
        is visible by its absence.
        """
        try:
            return self._providers.get_provider()
        except Exception:  # noqa: BLE001
            return None

    def _default_provider_name(self) -> str:
        """Name of the provider ``agent run`` would use, or "" if it cannot resolve."""
        provider = self._default_provider()
        return provider.name if provider is not None else ""

    def _autostart_row(self) -> AutostartRow:
        """
        Report whether the next non-headless ``agent run`` would start the logs viewer.

        Requested-vs-effective, for the same reason ``AGENT_USE_HOST_NETWORK`` is reported
        that way: the variable can be set and still not apply, which otherwise reads as
        the setting simply not working. Here the gate is the provider rather than the
        host — one whose statusline segment is fed from somewhere else has no use for the
        viewer and declines it.

        Note the polarity is the opposite of host networking's: this is an opt-out, so an
        unset variable means on, and only an explicit falsey value turns it off.
        """
        requested = optional_truthy_env(os.environ.get(AUTOSTART_LOGS_ENV, ""))
        provider = self._default_provider()
        # An unresolved provider gates nothing: the variable alone is all that can be said.
        declines = provider is not None and not provider.autostart_logs_viewer
        return AutostartRow(
            requested=requested,
            effective=requested is not False and not declines,
            declining_provider=provider.name if declines and provider is not None else "",
        )

    def _wrapper_row(self) -> WrapperRow:
        """Resolve the installed wrapper's git identity and interpreter, locally."""
        revision = self._updates.current_revision()
        return WrapperRow(
            branch=revision.branch,
            commit=revision.commit,
            describe=revision.describe,
            dirty=revision.dirty,
            python_version=platform.python_version(),
            python_pinned=self._pinned_python_version(),
        )

    @staticmethod
    def _pinned_python_version() -> str | None:
        """
        Read AGENT_PY_VERSION out of python-pin.env, or None if it cannot be read.

        Deliberately a two-line parse rather than anything that sources the file: this
        runs inside a read-only report, and the file is a plain list of KEY=value lines
        precisely so that both sh and this can read it without a shell.
        """
        try:
            text = PYTHON_PIN_FILE.read_text(encoding="utf-8")
        except OSError:
            return None
        for line in text.splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "AGENT_PY_VERSION":
                return value.strip() or None
        return None

    def _project_image_row(
        self,
        resolved: ResolvedImage | None,
        *,
        presence: ImagePresence,
        versions: ClaudeVersions,
    ) -> ProjectImageRow | None:
        """
        Describe the image this project's Dockerfile declares, or None when it declares none.

        The update flag reuses the report's single registry lookup rather than asking
        again, so a project image built from a stale base is flagged for the same reason
        and at the same cost as the base image itself.
        """
        if resolved is None:
            return None
        return ProjectImageRow(
            image=resolved.image,
            dockerfile=str(resolved.dockerfile),
            is_legacy=resolved.is_legacy,
            present=presence.project,
            claude_version=versions.project,
            claude_update_available=docker_utils.is_newer_version(
                versions.project, versions.latest
            ),
        )

    def _environment_row(
        self, *, presence: ImagePresence, versions: ClaudeVersions, network_present: bool
    ) -> EnvironmentRow:
        """
        Collect the host facts behind the most common launch surprises.

        ``AGENT_USE_HOST_NETWORK`` is reported as requested-vs-effective because it is
        silently ignored off WSL, which otherwise looks like the setting not working.
        """
        requested = is_truthy_env(os.environ.get(HOST_NETWORK_ENV, ""))
        day_start_overridden = bool(os.environ.get(DAY_START_ENV))
        day_start_timezone = None if day_start_overridden else os.environ.get(TIMEZONE_ENV) or None
        return EnvironmentRow(
            base_image=BASE_IMAGE_NAME,
            base_image_present=presence.base,
            base_image_version=versions.base,
            latest_claude_version=versions.latest,
            claude_update_available=docker_utils.is_newer_version(versions.base, versions.latest),
            network_name=SIDECAR_NETWORK_NAME,
            network_present=network_present,
            host_network_requested=requested,
            host_network_effective=requested and docker_utils.is_wsl(),
            day_start_hours=DAY_START_HOURS,
            day_start_overridden=day_start_overridden,
            day_start_timezone=day_start_timezone,
        )

    def _storage_row(self, *, logs_bytes: int | None) -> StorageRow:
        """Count registry entries, and record the logs footprint the pool measured."""
        registered = self._config.read_project_paths()
        return StorageRow(
            logs_bytes=logs_bytes,
            projects_registered=len(registered),
            projects_stale=len(self._config.stale_project_paths()),
        )
