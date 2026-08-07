# This file has been created with the assistance of an AI tool.
"""Agent launch orchestration domain service."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,
    AGENT_WRAP_MOUNT,
    GLOBAL_CONFIG_DIR,
    OPS_DIR,
    ROLE_LABEL,
    ROLE_VALUE,
    SIDECAR_NETWORK_NAME,
    STATE_FILES,
    TELEGRAM_IMAGE,
    TELEGRAM_SIDECAR_NAME,
    TOOL_DIR,
)
from agent_wrap.domain.launch.constants import (
    EXPECTED_QUEUE_DEPTH,
    HEADLESS_FLAGS,
    STATE_MOUNTS,
)
from agent_wrap.domain.launch.models import (
    DockerfileDirectives,
    HostNetworkResult,
    LaunchPreparation,
    SidecarAssembly,
)
from agent_wrap.exceptions import ProviderNotFoundError, SecretNotFoundError
from agent_wrap.lib import docker_utils
from agent_wrap.lib.priority_lock import Priority, priority_lock
from agent_wrap.lib.utils import generate_uuid, is_truthy_env, sanitize_name

if TYPE_CHECKING:
    from typing import TextIO

    from agent_wrap.domain.build.service import BuildService
    from agent_wrap.domain.config.service import ConfigService
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.providers.service import ProviderService
    from agent_wrap.domain.secrets.service import SecretsService
    from agent_wrap.domain.sidecars.base import Sidecar
    from agent_wrap.domain.sidecars.service import (
        SidecarService,
        SidecarTracker,
        TelegramSidecar,
    )
    from agent_wrap.domain.updates.service import UpdateService


class LaunchService:
    """Prepares and launches a Claude Code Docker container."""

    def __init__(  # noqa: PLR0913, PLR0917
        self,
        config_service: ConfigService,
        secrets_service: SecretsService,
        update_service: UpdateService,
        provider_service: ProviderService,
        sidecar_service: SidecarService,
        build_service: BuildService,
        display_service: DisplayService,
    ) -> None:
        self._config = config_service
        self._secrets = secrets_service
        self._updates = update_service
        self._provider_service = provider_service
        self._sidecar_service = sidecar_service
        self._build_service = build_service
        self._display = display_service

    # Public entry point

    def launch(self, *, use_base: bool, claude_args: list[str]) -> int:
        """Full launch pipeline: update check, image resolve, sidecar setup, docker run, cleanup."""
        headless = self._is_headless(claude_args)

        if not headless and self._updates.check_updates():
            return 0

        try:
            resolved = self._build_service.resolve_image(use_base=use_base)
        except SystemExit as e:
            self._display.error(str(e))
            return 1

        if not docker_utils.image_exists(resolved.image):
            self._display.error(self._get_image_missing_error(resolved.image, use_base=use_base))
            return 1

        agent_user, port_args, extra_run_args = self._parse_dockerfile_directives(
            resolved.dockerfile
        )

        agent_network = self._extract_network(extra_run_args)
        use_host_net, host_net_args, port_args = self._resolve_host_network(
            agent_network, port_args
        )
        claude_home = f"/home/{agent_user}"
        agent_name = self._resolve_agent_name(use_base=use_base, cwd=Path.cwd())

        instance_id = f"{agent_name}-{generate_uuid()}"

        try:
            sidecars, per_sidecar_secrets, telegram_available = self._assemble_sidecars(
                agent_name, instance_id, headless=headless
            )
            provider = self._provider_service.get_provider()
        except ProviderNotFoundError as e:
            self._display.error(str(e))
            return 1

        tracker = self._sidecar_service.create_tracker(TOOL_DIR)

        self._display.banner(f"Agent instance: {instance_id}")

        running_handles: dict[str, TextIO | None] = {}
        try:
            provider_run_args, running_handles = self._prepare_for_launch(
                sidecars,
                tracker,
                net=(use_host_net, agent_network),
                instance_id=instance_id,
                telegram_available=telegram_available,
                per_sidecar_secrets=per_sidecar_secrets,
            )

            self._display.banner(
                f"Launching Claude (Image: {resolved.image}, Config: {GLOBAL_CONFIG_DIR})"
            )

            cmd = [
                "docker",
                "run",
                "--rm",
                *docker_utils.get_tty_args(),
                *docker_utils.get_user_args(),
                *self._build_volume_mounts(claude_home),
                *self._build_env_args(
                    agent_name,
                    instance_id,
                    claude_home,
                    disable_nonessential_traffic=provider.disable_nonessential_traffic,
                ),
                *self._build_agent_labels(instance_id),
                *self._build_wslg_args(),
                *provider_run_args,
                *port_args,
                *host_net_args,
                *extra_run_args,
                resolved.image,
                *claude_args,
            ]

            result = subprocess.run(cmd)
            return result.returncode
        finally:
            self._release_sidecars(sidecars, tracker, instance_id, running_handles)

    # Instance helpers (shared utility methods)

    def _extract_network(self, extra_run_args: list[str]) -> str | None:
        """Extract --network value from a list of docker run flags."""
        for i, arg in enumerate(extra_run_args):
            if arg in ("--network", "--net"):
                if i + 1 < len(extra_run_args):
                    return extra_run_args[i + 1]
            elif arg.startswith(("--network=", "--net=")):
                return arg.split("=", 1)[1]
        return None

    def _is_headless(self, claude_args: list[str]) -> bool:
        """Report whether Claude Code is launched in a mode that won't use the sidecar."""
        return any(arg in HEADLESS_FLAGS for arg in claude_args)

    def _resolve_agent_name(self, *, use_base: bool, cwd: Path) -> str:
        """Determine agent name from Dockerfile.agent or directory name."""
        if use_base:
            return sanitize_name(cwd.name) or "agent"

        dockerfile_agent = cwd / "Dockerfile.agent"
        if not dockerfile_agent.is_file():
            return sanitize_name(cwd.name) or "agent"

        with open(dockerfile_agent) as f:
            for line in f:
                if match := re.match(r"^#\s*agent-name:\s*(\S+)", line.strip()):
                    return match.group(1)

        return sanitize_name(cwd.name) or "agent"

    def _build_wslg_args(self) -> list[str]:
        """Build WSLg-related volume mounts and env vars."""
        if not Path("/mnt/wslg").is_dir():
            return []
        return [
            "-v",
            "/mnt/wslg/runtime-dir:/mnt/wslg/runtime-dir",
            "-v",
            "/mnt/wslg/.X11-unix:/tmp/.X11-unix",
            "-v",
            f"{OPS_DIR}/wl-paste-shim:/usr/local/bin/wl-paste:ro",
            "-e",
            "DISPLAY",
            "-e",
            "WAYLAND_DISPLAY",
            "-e",
            "XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir",
        ]

    def _parse_dockerfile_directives(
        self,
        resolved_dockerfile: Path,
    ) -> DockerfileDirectives:
        """Parse Dockerfile.agent directives."""
        agent_user = "ubuntu"
        port_args: list[str] = []
        extra_run_args: list[str] = []
        if resolved_dockerfile.name == "Dockerfile.agent":
            info = self._build_service.parse_dockerfile_agent(resolved_dockerfile)
            agent_user = info.agent_user
            for port in info.expose_ports:
                port_args.extend(["-p", f"127.0.0.1:{port}:{port}"])
            extra_run_args = info.extra_run_args
        return DockerfileDirectives(agent_user, port_args, extra_run_args)

    def _build_env_args(
        self,
        agent_name: str,
        instance_id: str,
        claude_home: str,
        *,
        disable_nonessential_traffic: bool,
    ) -> list[str]:
        """Build -e flags for the docker run command."""
        disabler_flag = (
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
            if disable_nonessential_traffic
            else "DISABLE_AUTOUPDATER"
        )

        args = [
            "-e",
            f"{disabler_flag}=1",
            "-e",
            f"AGENT_NAME={agent_name}",
            "-e",
            f"AGENT_INSTANCE_ID={instance_id}",
            "-e",
            f"TERM={os.environ.get('TERM', 'xterm-256color')}",
            "-e",
            f"COLORTERM={os.environ.get('COLORTERM', 'truecolor')}",
            "-e",
            f"HOME={claude_home}",
        ]
        for flag in ("ENABLE_PROMPT_CACHING_1H",):
            flag_value = os.environ.get(flag, None)
            if flag_value is None:
                continue
            args.extend(["-e", f"{flag}={flag_value}"])
        return args

    def _build_volume_mounts(self, claude_home: str) -> list[str]:
        """Build all -v mount flags for the docker run command."""
        mounts: list[str] = []
        cwd = Path.cwd()

        mounts.extend(
            [
                "-v",
                f"{GLOBAL_CONFIG_DIR}/.claude.json:{claude_home}/.claude.json",
                "-v",
                f"{GLOBAL_CONFIG_DIR}/.claude:{claude_home}/.claude",
                "-v",
                f"{cwd}:/workspace",
            ]
        )

        for name, dest in STATE_MOUNTS.items():
            mounts.extend(["-v", f"{cwd}/.claude/{name}:{claude_home}/.claude/{dest}"])

        for name in STATE_FILES:
            mounts.extend(["-v", f"{cwd}/.claude/{name}:{claude_home}/.claude/{name}"])

        mounts.extend(["-v", f"{OPS_DIR}:{AGENT_WRAP_MOUNT}:ro"])

        return mounts

    def _build_agent_labels(self, instance_id: str) -> list[str]:
        """Build the agent container's --label / --name flags."""
        if not instance_id:
            return []
        return [
            "--label",
            f"{ROLE_LABEL}={ROLE_VALUE}",
            "--label",
            f"agent-wrap.instance-id={instance_id}",
            "--name",
            f"claude-agent-{instance_id}",
        ]

    def _sidecar_lock_timeout(self, sidecars: list[Sidecar], queue_depth: int) -> float:
        """
        Total seconds a launcher waits for the shared sidecar lock.

        Sized from *this* launch's own sidecars, while the lock is global — so a herd
        spanning several providers can make one launcher wait behind a foreign cold
        start too. The budget absorbs it with room to spare: at defaults it is
        120 + 128·2 (LiteLLM) + 45 + 128·2 (Telegram) = 677 s against a worst case of
        one foreign 120 s cold start plus this launch's own 165 s of work, and it still
        holds at AGENT_EXPECTED_QUEUE_DEPTH=1 (169 s vs. 120 s).
        """
        return sum(sc.cold_start_time + queue_depth * sc.short_circuit_time for sc in sidecars)

    # Instance methods (use injected services)

    def _resolve_sidecar_secrets(
        self,
        sidecar_name: str,
        required: list[tuple[str, str]],
        *,
        optional: bool,
        headless: bool,
    ) -> dict[str, str] | None:
        """Atomically resolve all secrets for a sidecar."""
        prompt_on_missing = sys.stdin.isatty() and not optional and not headless

        try:
            return {
                key: self._secrets.read(
                    f"{sidecar_name}:{key}", desc, prompt_on_missing=prompt_on_missing
                )
                for key, desc in required
            }
        except SecretNotFoundError:
            if optional:
                return None

            self._display.error(
                f"Secrets for '{sidecar_name}' not found. Run 'agent secrets set {sidecar_name}'."
            )
            raise SystemExit(1) from None

    def _resolve_host_network(
        self,
        agent_network: str | None,
        port_args: list[str],
    ) -> HostNetworkResult:
        """Resolve AGENT_USE_HOST_NETWORK env var."""
        env_val = os.environ.get("AGENT_USE_HOST_NETWORK", "")
        if not is_truthy_env(env_val):
            return HostNetworkResult(use_host_net=False, host_net_args=[], port_args=port_args)

        if not docker_utils.is_wsl():
            self._display.warning("AGENT_USE_HOST_NETWORK ignored — only honored on WSL hosts.")
            return HostNetworkResult(use_host_net=False, host_net_args=[], port_args=port_args)

        if agent_network:
            self._display.warning(
                "AGENT_USE_HOST_NETWORK ignored — Dockerfile.agent already "
                "specifies --network via agent-run-args."
            )
            return HostNetworkResult(use_host_net=False, host_net_args=[], port_args=port_args)

        if port_args:
            self._display.warning(
                "AGENT_USE_HOST_NETWORK is on — EXPOSE port mappings "
                "skipped. Services bind on the WSL distro's interfaces directly; "
                "ensure they listen on 127.0.0.1 to avoid LAN exposure."
            )
        return HostNetworkResult(
            use_host_net=True, host_net_args=["--network", "host"], port_args=[]
        )

    def _release_sidecars(
        self,
        sidecars: list[Sidecar],
        tracker: SidecarTracker,
        instance_id: str,
        running_handles: dict[str, TextIO | None],
    ) -> None:
        """
        Per-container last-light-out teardown.

        Every registration is dropped first, so a concurrent stopper sees this agent as
        gone on every container at once. Then, under the yield-to-starters lock, each
        declared sidecar is released only if no *other* agent still holds its container
        — so an agent on one provider stops that provider's sidecar while agents on
        other providers keep theirs, and the shared Telegram container survives until
        the last agent anywhere exits.

        *running_handles* is keyed by container name (not by ``Sidecar``, unlike
        ``per_sidecar_secrets``) and may be empty when ``ensure()`` failed mid-launch.
        """
        for sidecar in sidecars:
            container = sidecar.container_name
            tracker.clear_running(running_handles.get(container), container, instance_id)
        for sidecar in reversed(sidecars):
            self._safe_sidecar_on_exit(sidecar)
        with priority_lock(
            Priority.LO,
            lock_path=tracker.lock_path,
            waiters_dir=tracker.start_waiters_dir,
            instance_id=instance_id,
        ):
            for sidecar in reversed(sidecars):
                if not tracker.has_live_runners(sidecar.container_name, exclude_id=instance_id):
                    sidecar.release()

    # Private helpers

    def _telegram_sidecar(
        self,
        *,
        agent_name: str,
        instance_id: str,
        headless: bool,
    ) -> TelegramSidecar:
        if headless:
            self._display.warning("headless mode — Telegram sidecar will not be started.")
        return self._sidecar_service.create_telegram_sidecar(
            image=TELEGRAM_IMAGE,
            container_name="agent-wrap-telegram",
            network_name=SIDECAR_NETWORK_NAME,
            internal_port=6837,
            agent_name=agent_name,
            instance_id=instance_id,
            health_timeout_sec=30,
            cold_start_time=45.0,
            short_circuit_time=2.0,
            log_dir=AGENT_LAUNCHES_DIR / "telegram-sidecar-logs",
            headless=headless,
        )

    def _assemble_sidecars(
        self,
        agent_name: str,
        instance_id: str,
        *,
        headless: bool,
    ) -> SidecarAssembly:
        provider = self._provider_service.get_provider()
        sidecars: list[Sidecar] = [provider.sidecar()]
        per_sidecar: dict[Sidecar, dict[str, str]] = {}
        for sc in sidecars:
            result = self._resolve_sidecar_secrets(
                provider.name, sc.required_secrets(), optional=False, headless=headless
            )
            assert result is not None
            per_sidecar[sc] = result

        telegram_available = False
        tg_secrets = self._resolve_sidecar_secrets(
            TELEGRAM_SIDECAR_NAME,
            self._sidecar_service.telegram_required_secrets(),
            optional=True,
            headless=headless,
        )
        if tg_secrets:
            tg_sidecar = self._telegram_sidecar(
                agent_name=agent_name,
                instance_id=instance_id,
                headless=headless,
            )
            sidecars.append(tg_sidecar)
            per_sidecar[tg_sidecar] = tg_secrets
            telegram_available = True

        return SidecarAssembly(sidecars, per_sidecar, telegram_available)

    def _expected_queue_depth(self) -> int:
        raw = os.environ.get("AGENT_EXPECTED_QUEUE_DEPTH")
        if raw:
            try:
                value = int(raw)
            except ValueError:
                value = 0
            if value > 0:
                return value
        return EXPECTED_QUEUE_DEPTH

    def _prepare_config(self, *, telegram_available: bool) -> None:
        cwd = Path.cwd()
        self._config.prepare_global_config(telegram_available=telegram_available)
        self._config.prepare_project_dirs(cwd, tuple(STATE_MOUNTS.keys()), STATE_FILES)
        self._config.link_litellm_logs(cwd)
        self._config.record_project()

    def _prepare_for_launch(  # noqa: PLR0913
        self,
        sidecars: list[Sidecar],
        tracker: SidecarTracker,
        *,
        net: tuple[bool, str | None],
        instance_id: str,
        telegram_available: bool,
        per_sidecar_secrets: dict[Sidecar, dict[str, str]],
    ) -> LaunchPreparation:
        use_host_net, agent_network = net
        for sidecar in sidecars:
            sidecar.prepare()

        run_args: list[str] = []
        running_handles: dict[str, TextIO | None] = {}
        timeout = self._sidecar_lock_timeout(sidecars, self._expected_queue_depth())
        with priority_lock(
            Priority.HI,
            lock_path=tracker.lock_path,
            waiters_dir=tracker.start_waiters_dir,
            instance_id=instance_id,
            timeout=timeout,
        ):
            self._prepare_config(telegram_available=telegram_available)
            for sidecar in sidecars:
                run_args += sidecar.ensure(
                    use_host_net=use_host_net,
                    agent_network=agent_network,
                    secrets=per_sidecar_secrets[sidecar],
                )
            # Registered together, as the last action under the lock: if any ensure()
            # above raised, nothing is registered and teardown still runs for every
            # sidecar this launch declared.
            running_handles = {
                sidecar.container_name: tracker.register_running(
                    sidecar.container_name, instance_id
                )
                for sidecar in sidecars
            }
        return LaunchPreparation(run_args, running_handles)

    def _safe_sidecar_on_exit(self, sidecar: Sidecar) -> None:
        try:
            sidecar.on_exit()
        except Exception:  # noqa: BLE001
            # on_exit is a best-effort cleanup hook — failures must not block the
            # release of shared resources (lock, network, container).
            self._display.warning(
                f"sidecar.on_exit() failed for {type(sidecar).__name__}, continuing with release"
            )

    def _get_image_missing_error(self, image: str, *, use_base: bool) -> str:
        if use_base:
            return f"Error: Base image '{image}' not found. Run 'agent rebuild --full' to build it."
        return (
            f"Error: Image '{image}' not found. Run 'agent rebuild' in this directory to build it."
        )
