# This file has been created with the assistance of an AI tool.
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
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

from agent_wrap.lib.docker_utils import docker_run, get_user_args, image_exists
from agent_wrap.lib.spinner import PollResult, Spinner
from agent_wrap.sidecars.base import Sidecar

_SPINNER = Spinner("telegram-sidecar")


@dataclass(frozen=True)
class TelegramSidecarConfig:
    """Immutable configuration for a ``TelegramSidecar``."""

    # --- identity ---
    image: str
    container_name: str
    network_name: str
    internal_port: int

    # --- credentials (for the sidecar container only, never passed to the agent) ---
    bot_token: str
    chat_id: str

    # --- per-run identity (for /register and /unregister on the sidecar) ---
    agent_name: str
    instance_id: str

    # --- health / concurrency timing ---
    health_timeout_sec: int
    #: Seconds a cold start takes (docker run + health poll).
    cold_start_time: float
    #: Seconds one agent takes to walk the lock on the hot path.
    short_circuit_time: float


class TelegramSidecar(Sidecar):
    """The shared Telegram decision sidecar container, managed as a singleton sidecar."""

    def __init__(self, config: TelegramSidecarConfig) -> None:
        self.config = config
        self._auth_token: str = ""

    # --- Sidecar interface properties ---

    @property
    def cold_start_time(self) -> float:
        return self.config.cold_start_time

    @property
    def short_circuit_time(self) -> float:
        return self.config.short_circuit_time

    # --- Public: prepare / ensure ---

    def prepare(self) -> None:
        """Pull the sidecar image lock-free, before the runner takes the shared lock."""
        if image_exists(self.config.image):
            return
        print(
            f"telegram-sidecar: pulling {self.config.image} (first run, may take a moment)…",
            file=sys.stderr,
        )
        _, rc = docker_run("pull", self.config.image, capture=False, timeout=600)
        if rc != 0:
            msg = f"telegram-sidecar: failed to pull image {self.config.image}"
            raise SystemExit(msg)

    def ensure(
        self,
        *,
        use_host_net: bool,
        agent_network: str | None,
    ) -> list[str]:
        """
        Ensure the sidecar is running + healthy and return the agent's docker run flags.

        Runs under the runner's shared lock. Returns env var flags
        (``TELEGRAM_SIDECAR_URL``, ``TELEGRAM_SIDECAR_TOKEN``) plus network
        connectivity flags so the agent can reach the sidecar.
        """
        agent_in_host_netns = bool(use_host_net) or agent_network == "host"

        self._ensure_network()

        if self._is_running():
            # Sidecar already up — first-launch-wins on the container, but each
            # agent run gets its own auth token via /register.
            pass
        else:
            self._start()
            if not self._health_poll():
                print(
                    "telegram-sidecar: health check failed; recent logs:",
                    file=sys.stderr,
                )
                docker_run("logs", "--tail", "50", self.config.container_name)
                raise SystemExit(1)

        self._auth_token = self._register()
        if not self._auth_token:
            print(
                "telegram-sidecar: /register returned no auth_token; "
                "notifications will be unavailable",
                file=sys.stderr,
            )

        # Attach sidecar to agent's custom network if needed
        if agent_network and agent_network not in ("host", "none", self.config.network_name):
            self._attach_to_network(agent_network)

        return self._build_connectivity_args(agent_in_host_netns=agent_in_host_netns)

    # --- Public: release ---

    def release(self) -> None:
        """
        Stop the sidecar container.

        Runs under the runner's shared lock, only after its ``SidecarTracker``
        decided the run may stop. Idempotent — a no-op when the container is
        not running.
        """
        self._unregister()
        if self._is_running():
            _SPINNER.spin_while(
                message="stopping…",
                done_message="stopped",
                work=lambda: docker_run("stop", self.config.container_name),
            )

    # --- Internal: network ---

    def _ensure_network(self) -> None:
        _, rc = docker_run("network", "inspect", self.config.network_name)
        if rc == 0:
            return
        _, rc = docker_run("network", "create", self.config.network_name)
        if rc != 0:
            msg = f"telegram-sidecar: failed to create docker network {self.config.network_name}"
            raise SystemExit(msg)

    def _attach_to_network(self, network: str) -> None:
        _, rc = docker_run("network", "inspect", network)
        if rc != 0:
            msg = f"telegram-sidecar: network '{network}' (from agent-run-args) does not exist"
            raise SystemExit(msg)

        if self._is_on_network(network):
            return

        _, rc = docker_run("network", "connect", network, self.config.container_name)
        if rc != 0:
            msg = (
                f"telegram-sidecar: failed to attach "
                f"{self.config.container_name} to network '{network}'"
            )
            raise SystemExit(msg)

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

        cmd = [
            "run",
            "-d",
            "--rm",
            "--name",
            self.config.container_name,
            "--network",
            self.config.network_name,
            "-p",
            f"127.0.0.1:{self.config.internal_port}:{self.config.internal_port}",
            *get_user_args(),
            "-e",
            f"TELEGRAM_BOT_TOKEN={self.config.bot_token}",
            "-e",
            f"TELEGRAM_CHAT_ID={self.config.chat_id}",
            self.config.image,
        ]
        _, rc = docker_run(*cmd)
        if rc != 0:
            msg = f"telegram-sidecar: failed to start {self.config.container_name}"
            raise SystemExit(msg)

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

        return _SPINNER.poll_until(
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
        req = urllib.request.Request(  # noqa: S310
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
                    print(
                        "telegram-sidecar: /register returned no auth_token",
                        file=sys.stderr,
                    )
                return token
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            print(
                f"telegram-sidecar: /register failed ({exc})",
                file=sys.stderr,
            )
            return ""

    def _unregister(self) -> None:
        """POST /unregister — tear down this agent's session. Best-effort."""
        if not self._auth_token:
            return
        url = f"http://127.0.0.1:{self.config.internal_port}/unregister"
        req = urllib.request.Request(  # noqa: S310
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
