# This file has been edited with the assistance of an AI tool.
"""
The Telegram decision sidecar as a ``Sidecar``.

Implements the full sidecar lifecycle for a shared container that fronts the
Telegram Bot API — handling interactive permission decisions (Allow/Deny
buttons) and fire-and-forget notifications. The agent never sees the raw bot
token; it receives only a sidecar URL and a per-run auth token.

``TelegramSidecar`` is configured by an immutable ``TelegramSidecarConfig``.
Locking and the start/stop decision are the runner's concern (one shared lock
+ one ``SidecarTracker``); this class only ensures/stops its container.
"""

from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.sidecars.models import TelegramSidecarConfig

from agent_wrap.constants import TELEGRAM_SIDECAR_LABEL, PollResult
from agent_wrap.domain.sidecars.base import Sidecar
from agent_wrap.lib.docker_utils import docker_run, get_user_args, image_exists


class TelegramSidecar(Sidecar):
    """The shared Telegram decision sidecar container, managed as a singleton sidecar."""

    def __init__(
        self,
        config: TelegramSidecarConfig,
        display_service: DisplayService,
    ) -> None:
        self.config = config
        self._display = display_service
        self._auth_token: str = ""
        self._bot_token: str = ""
        self._chat_id: str = ""

    # --- Sidecar interface properties ---

    @property
    def container_name(self) -> str:
        # One container name for every provider, so the runner refcounts this sidecar
        # across all of them — correct, since it is genuinely one shared container.
        return self.config.container_name

    @property
    def cold_start_time(self) -> float:
        return self.config.cold_start_time

    @property
    def short_circuit_time(self) -> float:
        return self.config.short_circuit_time

    @classmethod
    def required_secrets(cls) -> list[tuple[str, str]]:
        return [
            ("TelegramBotToken", "Telegram Bot Token (from @BotFather)"),
            ("TelegramChatId", "Telegram Chat ID (numeric user or group ID)"),
        ]

    # --- Public: prepare / ensure ---

    def prepare(self) -> None:
        """Pull the sidecar image lock-free, before the runner takes the shared lock."""
        if self.config.headless:
            return  # headless run never uses the sidecar — don't pull
        if image_exists(self.config.image):
            return
        self._display.warning(
            f"{TELEGRAM_SIDECAR_LABEL}: pulling {self.config.image} (first run, may take a moment)…"
        )
        _, rc = docker_run("pull", self.config.image, capture=False, timeout=600)
        if rc != 0:
            self._display.error(
                f"{TELEGRAM_SIDECAR_LABEL}: failed to pull image {self.config.image}"
            )
            raise SystemExit(1)

    def ensure(
        self,
        *,
        use_host_net: bool,
        agent_network: str | None,
        secrets: dict[str, str] | None = None,
    ) -> list[str]:
        """
        Ensure the sidecar is running + healthy and return the agent's docker run flags.

        Runs under the runner's shared lock. Returns env var flags
        (``TELEGRAM_SIDECAR_URL``, ``TELEGRAM_SIDECAR_TOKEN``) plus network
        connectivity flags so the agent can reach the sidecar.

        *secrets* carries the resolved credentials for this sidecar (simple keys).
        """
        if secrets:
            self._bot_token = secrets["TelegramBotToken"]
            self._chat_id = secrets["TelegramChatId"]

        if self.config.headless:
            # Headless run never fires hooks/permission prompts — don't start the
            # container or hand the agent any sidecar env. release() stays active
            # (it is _is_running()-gated) so this run still reaps the shared
            # singleton if it is the last one out.
            return []

        agent_in_host_netns = bool(use_host_net) or agent_network == "host"

        self._ensure_network()

        if self._is_running():
            # Sidecar already up — first-launch-wins on the container, but each
            # agent run gets its own auth token via /register.
            pass
        else:
            self._start()
            if not self._health_poll():
                self._display.error(f"{TELEGRAM_SIDECAR_LABEL}: health check failed; recent logs:")
                try:
                    # Stream the container's stdout+stderr straight through
                    # (capture=False): a startup crash writes its traceback to
                    # stderr, which a captured-stdout return value would drop.
                    docker_run("logs", "--tail", "50", self.config.container_name, capture=False)
                finally:
                    # Always tear down — the container is started without --rm
                    # (see _start) so its logs survive an early exit, but it must
                    # not outlive this failed launch even if streaming the logs
                    # above raised (e.g. BrokenPipeError on a closed pipe).
                    self._stop_and_remove()
                raise SystemExit(1)

        self._auth_token = self._register()
        if not self._auth_token:
            self._display.warning(
                f"{TELEGRAM_SIDECAR_LABEL}: /register returned no auth_token; "
                "notifications will be unavailable"
            )

        # Attach sidecar to agent's custom network if needed
        if agent_network and agent_network not in ("host", "none", self.config.network_name):
            self._attach_to_network(agent_network)

        return self._build_connectivity_args(agent_in_host_netns=agent_in_host_netns)

    # --- Public: release ---

    def release(self) -> None:
        """
        Gracefully stop and remove the sidecar container.

        Runs under the runner's shared lock, only after its ``SidecarTracker``
        decided the run may stop. Idempotent — a no-op when the container is
        not running. Removal is needed because the container is started without
        ``--rm`` (so a crash leaves logs to surface), and a successful run must
        not leave a stopped corpse behind.
        """
        if self._is_running():
            self._display.spin_while(
                label=TELEGRAM_SIDECAR_LABEL,
                message="stopping…",
                done_message="stopped",
                work=self._stop_and_remove,
            )

    def _stop_and_remove(self) -> None:
        """
        Gracefully stop, then remove, the sidecar container.

        ``stop`` (SIGTERM + grace period, not ``rm -f``/SIGKILL) so the sidecar's
        in-container cleanup stage runs. The container is started without ``--rm``
        (so a crash leaves logs to surface), so removal is explicit; the ``rm`` is
        plain (no ``-f``) and succeeds because ``stop`` has left the container
        exited. ``stop`` on an already-exited container is a quick no-op, so this
        is safe on both the health-failure path (crash corpse) and normal teardown.
        """
        docker_run("stop", self.config.container_name)
        docker_run("rm", self.config.container_name)

    # --- Internal: network ---

    def _ensure_network(self) -> None:
        _, rc = docker_run("network", "inspect", self.config.network_name)
        if rc == 0:
            return
        _, rc = docker_run("network", "create", self.config.network_name)
        if rc != 0:
            self._display.error(
                f"{TELEGRAM_SIDECAR_LABEL}: failed to create docker network "
                f"{self.config.network_name}"
            )
            raise SystemExit(1)

    def _attach_to_network(self, network: str) -> None:
        _, rc = docker_run("network", "inspect", network)
        if rc != 0:
            self._display.error(
                f"{TELEGRAM_SIDECAR_LABEL}: network '{network}' (from agent-run-args) "
                "does not exist"
            )
            raise SystemExit(1)

        if self._is_on_network(network):
            return

        _, rc = docker_run("network", "connect", network, self.config.container_name)
        if rc != 0:
            self._display.error(
                f"{TELEGRAM_SIDECAR_LABEL}: failed to attach "
                f"{self.config.container_name} to network '{network}'"
            )
            raise SystemExit(1)

    def _is_on_network(self, network: str) -> bool:
        fmt = "{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}"
        stdout, rc = docker_run("inspect", self.config.container_name, "--format", fmt)
        return rc == 0 and network in stdout.splitlines()

    def _sidecar_ip_on_network(self, network: str) -> str:
        fmt = (
            f'{{{{with index .NetworkSettings.Networks "{network}"}}}}{{{{.IPAddress}}}}{{{{end}}}}'
        )
        stdout, rc = docker_run("inspect", self.config.container_name, "--format", fmt)
        return stdout.strip() if rc == 0 else ""

    # --- Internal: container lifecycle ---

    def _is_running(self) -> bool:
        stdout, rc = docker_run(
            "container",
            "inspect",
            "-f",
            "{{.State.Running}}",
            self.config.container_name,
        )
        return rc == 0 and stdout.strip() == "true"

    def _start(self) -> None:
        # Reap any stopped container under our name
        _, rc = docker_run("container", "inspect", self.config.container_name)
        if rc == 0:
            docker_run("rm", "-f", self.config.container_name)

        # Prepare log directory and LOG_LOCATION
        dt = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        log_dir = self.config.log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_filename = f"{dt}.log"
        container_log_path = f"/var/log/telegram-sidecar/{log_filename}"

        cmd = [
            "run",
            "-d",
            # No --rm: a process that dies during startup (e.g. an unwritable
            # LOG_LOCATION mount) would otherwise be auto-removed before the
            # health poll can surface its logs. The stopped container is reaped
            # by the next _start (above) or by the health-failure path.
            "--name",
            self.config.container_name,
            "--network",
            self.config.network_name,
            "-p",
            f"127.0.0.1:{self.config.internal_port}:{self.config.internal_port}",
            *get_user_args(),
            "-e",
            f"TELEGRAM_BOT_TOKEN={self._bot_token}",
            "-e",
            f"TELEGRAM_CHAT_ID={self._chat_id}",
            "-e",
            f"LOG_LOCATION={container_log_path}",
            "-v",
            f"{log_dir}:/var/log/telegram-sidecar",
            self.config.image,
        ]
        _, rc = docker_run(*cmd)
        if rc != 0:
            self._display.error(
                f"{TELEGRAM_SIDECAR_LABEL}: failed to start {self.config.container_name}"
            )
            raise SystemExit(1)

    def _health_poll(self) -> bool:
        def poll() -> tuple[PollResult, str]:
            stdout, rc = docker_run(
                "inspect",
                self.config.container_name,
                "--format={{.State.Health.Status}}",
            )
            if rc != 0:
                return PollResult.FAILURE, ""
            status = stdout.strip()
            if status == "healthy":
                return PollResult.SUCCESS, status
            if status == "unhealthy" or not self._is_running():
                return PollResult.FAILURE, status
            return PollResult.PENDING, status

        return self._display.poll_until(
            label=TELEGRAM_SIDECAR_LABEL,
            poll=poll,
            message="waiting for healthy",
            done_message="ready",
            timeout=self.config.health_timeout_sec,
        )

    # --- Internal: HTTP calls to sidecar ---

    def _register(self) -> str:
        """POST /register — obtain an auth token for this agent run."""
        url = f"http://127.0.0.1:{self.config.internal_port}/register"
        body = json.dumps(
            {"agent_id": self.config.instance_id, "agent_name": self.config.agent_name}
        ).encode()
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                data = json.loads(resp.read())
                token = data.get("auth_token", "")
                if not token:
                    self._display.warning(
                        f"{TELEGRAM_SIDECAR_LABEL}: /register returned no auth_token"
                    )
                return token
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            self._display.warning(f"{TELEGRAM_SIDECAR_LABEL}: /register failed ({exc})")
            return ""

    def _unregister(self) -> None:
        """POST /unregister — tear down this agent's session. Best-effort."""
        if not self._auth_token:
            return
        url = f"http://127.0.0.1:{self.config.internal_port}/unregister"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._auth_token}",
            },
            method="POST",
        )
        with contextlib.suppress(urllib.error.URLError, urllib.error.HTTPError, OSError):
            urllib.request.urlopen(req, timeout=5)  # noqa: S310

    # --- Internal: connectivity ---

    def _build_connectivity_args(self, *, agent_in_host_netns: bool) -> list[str]:
        """
        Build docker run flags so the agent container can reach this sidecar.

        Returns env var flags (``TELEGRAM_SIDECAR_URL``, ``TELEGRAM_SIDECAR_TOKEN``)
        and network connectivity flags.
        """
        args: list[str] = [
            "-e",
            (
                f"TELEGRAM_SIDECAR_URL="
                f"http://{self.config.container_name}:"
                f"{self.config.internal_port}"
            ),
        ]
        if self._auth_token:
            args.extend(["-e", f"TELEGRAM_SIDECAR_TOKEN={self._auth_token}"])

        if agent_in_host_netns:
            sidecar_ip = self._sidecar_ip_on_network(self.config.network_name)
            if sidecar_ip:
                args.extend(["--add-host", f"{self.config.container_name}:{sidecar_ip}"])

        return args

    def on_exit(self) -> None:
        self._unregister()
