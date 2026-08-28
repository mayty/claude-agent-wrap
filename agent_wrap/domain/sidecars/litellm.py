# This file has been created with the assistance of an AI tool.
"""
The LiteLLM proxy as a ``Sidecar``.

Implements the full sidecar lifecycle: lazy start, health polling, network-mode
detection, port resolution, master key minting/recovery, and the connectivity matrix
for agent↔sidecar communication across network namespaces.

Each provider gets its own container (named after the provider), so agents on
different providers run side by side. Two things follow from that and are resolved the
same way — mint on cold start, recover from the running container afterwards, because
the container is the single source of truth and needs no state file:

* the **master key**, in ``-e LITELLM_MASTER_KEY``;
* the **port**, in ``-e AGENT_WRAP_SIDECAR_PORT``. It cannot be a constant: in
  host-network mode the container's port *is* a host port, so a second provider's
  sidecar would collide. Cold start scans upward from the provider's preferred base.

``LiteLLMSidecar`` is a pure mechanism configured by an immutable
``LiteLLMSidecarConfig`` — the owning provider supplies the image pin, resolved
on-disk paths, and the provider-specific behavior hooks (upstream auth env, agent
env, key approval). It holds no back-reference to the provider, so it is unit-testable
on its own. Locking and the start/stop decision are the runner's concern (one shared
lock + one ``SidecarTracker``, which refcounts per container name); this class only
ensures/stops its container.
"""

from __future__ import annotations

from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    LITELLM_SIDECAR_LABEL,
    PORT_SCAN_LIMIT,
    SIDECAR_PORT_ENV,
    SIDECAR_PROVIDER_ENV,
    PollResult,
)
from agent_wrap.domain.sidecars.base import Sidecar
from agent_wrap.lib.docker_utils import (
    docker_run,
    get_user_args,
    image_exists,
    network_exists,
)
from agent_wrap.lib.net import find_free_port
from agent_wrap.lib.path_hash import project_path_hash
from agent_wrap.lib.utils import generate_uuid

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.sidecars.models import LiteLLMSidecarConfig


class LiteLLMSidecar(Sidecar):
    """One provider's LiteLLM proxy container, managed as a per-provider singleton."""

    def __init__(
        self,
        config: LiteLLMSidecarConfig,
        display_service: DisplayService,
    ) -> None:
        self.config = config
        self._display = display_service
        self._master_key: str = ""
        #: Port this container actually listens on — scanned at cold start, recovered
        #: from the running container on the hot path. Zero until ``ensure()`` resolves
        #: it; read it through the ``port`` property, never ``config.internal_port``
        #: (which is only the preferred base).
        self._port: int = 0

    @property
    def cold_start_time(self) -> float:
        return self.config.cold_start_time

    @property
    def short_circuit_time(self) -> float:
        return self.config.short_circuit_time

    # Convenience accessors mirroring the old provider attributes.
    @property
    def container_name(self) -> str:
        return self.config.container_name

    @property
    def network_name(self) -> str:
        return self.config.network_name

    @property
    def internal_port(self) -> int:
        """The provider's *preferred base* port — not necessarily the resolved one."""
        return self.config.internal_port

    @property
    def port(self) -> int:
        """The port this container listens on, once ``ensure()`` has resolved it."""
        return self._port

    @property
    def image(self) -> str:
        return self.config.image

    @property
    def health_timeout_sec(self) -> int:
        return self.config.health_timeout_sec

    @property
    def health_endpoint(self) -> str:
        return self.config.health_endpoint

    @property
    def _label(self) -> str:
        """Display label naming the provider — two sidecars may be up at once."""
        return f"{LITELLM_SIDECAR_LABEL} ({self.config.provider_name})"

    def required_secrets(self) -> list[tuple[str, str]]:
        return list(self.config.required_secrets)

    # --- Public: prepare / ensure ---

    def prepare(self) -> None:
        """Pull the image lock-free, before the runner takes the shared lock."""
        # A cold pull (up to several minutes) must never run under the lock, or the
        # rest of a concurrent launch herd would block on it.
        self._ensure_image()

    def ensure(
        self,
        *,
        use_host_net: bool,
        agent_network: str | None,
        secrets: dict[str, str] | None = None,
    ) -> list[str]:
        if agent_network == "bridge":
            self._display.error(
                f"{self._label}: --network bridge is not supported "
                "(Docker's default bridge has no embedded DNS).\n"
                "Use a user-defined network (`docker network create <name>`) "
                "or remove --network from agent-run-args to use agent-wrap-net."
            )
            raise SystemExit(1)

        agent_in_host_netns = bool(use_host_net) or agent_network == "host"

        # Runs under the runner's shared lock (held across the whole launch), so the
        # start decision + health poll are atomic against every concurrent launcher.
        self._ensure_network()
        sidecar_mode = self._ensure_sidecar(
            use_host_net=use_host_net,
            secrets=secrets or {},
        )

        # Attach sidecar to agent's custom network if needed
        if (
            sidecar_mode != "host"
            and agent_network
            and agent_network not in ("host", "none", self.network_name)
        ):
            self._attach_to_network(agent_network)

        return self._build_connectivity_args(
            sidecar_mode, agent_in_host_netns=agent_in_host_netns, agent_network=agent_network
        )

    def _ensure_sidecar(self, *, use_host_net: bool, secrets: dict[str, str]) -> str:
        """Ensure the sidecar is running + healthy. Returns its network mode."""
        # Migration: sidecar from before agent-wrap-net refactor
        if (
            self._is_running()
            and not self._is_on_network(self.network_name)
            and not self._is_on_network("host")
        ):
            self._display.warning(
                f"{self._label}: existing sidecar predates agent-wrap-net; restarting"
            )
            docker_run("stop", self.container_name)

        if self._is_running():
            # First-launch-wins (per provider): inherit the running mode, key and port.
            # The key was already approved by whoever started this sidecar (on_started),
            # so do not re-approve here. Re-scanning for a port would be wrong — the
            # container is already bound to the one it recorded.
            sidecar_mode = "host" if self._is_on_network("host") else "bridge"
            self._master_key = self._recover_master_key()
            self._port = self._recover_port()
        else:
            sidecar_mode = "host" if use_host_net else "bridge"
            self._master_key = self._generate_master_key()
            # Safe under the runner's shared lock, which serializes every launch's scan
            # against every other. A foreign process stealing the port between the probe
            # and uvicorn's bind surfaces as the health-poll failure handled below.
            self._port = find_free_port(self.config.internal_port, PORT_SCAN_LIMIT)
            self._start(secrets, self._master_key, sidecar_mode)
            self.config.on_started(self._master_key)
            if not self._health_poll():
                # Leave the unhealthy container in place and raise: release() (which
                # always runs — see run.py) is the single home for the stop, and it
                # needs the container present to recover the key for un-approval. A
                # later cold start's _start() reaps it via `rm -f`.
                self._display.error(f"{self._label}: health check failed; recent logs:")
                # Stream stdout+stderr through (capture=False): the failure mode
                # here is usually unhealthy-but-alive (config error, proxy still
                # running), so the container — and its logs — are still present.
                # capture=False also lets a stderr traceback through, which a
                # captured-stdout return value would have dropped.
                docker_run("logs", "--tail", "50", self.container_name, capture=False)
                raise SystemExit(1)

        return sidecar_mode

    def _build_connectivity_args(
        self,
        sidecar_mode: str,
        *,
        agent_in_host_netns: bool,
        agent_network: str | None,
    ) -> list[str]:
        """Build env var flags and connectivity args for the agent container."""
        base_url = f"http://{self.container_name}:{self.port}"
        agent_env = dict(self.config.get_agent_env(self._master_key, base_url))

        # Inject the per-project log discriminator as a custom request header.
        # Claude Code forwards ANTHROPIC_CUSTOM_HEADERS verbatim on every upstream
        # call; the sidecar callback reads x-agent-wrap-log-prefix to route logs to
        # the right project subtree (see callback.py). The provider half of the
        # discriminator is fixed per sidecar and travels via AGENT_WRAP_PROVIDER.
        header = f"x-agent-wrap-log-prefix: {project_path_hash(Path.cwd())}"
        existing = agent_env.get("ANTHROPIC_CUSTOM_HEADERS")
        agent_env["ANTHROPIC_CUSTOM_HEADERS"] = f"{existing}\n{header}" if existing else header

        env_args: list[str] = []
        for key, value in agent_env.items():
            env_args.extend(["-e", f"{key}={value}"])

        if sidecar_mode == "host":
            if agent_in_host_netns:
                return [*env_args, "--add-host", f"{self.container_name}:127.0.0.1"]
            return [*env_args, "--add-host", f"{self.container_name}:host-gateway"]

        if agent_in_host_netns:
            sidecar_ip = self._sidecar_ip_on_network(self.network_name)
            if not sidecar_ip:
                self._display.error(
                    f"{self._label}: sidecar has no IP on {self.network_name} "
                    "— was it disconnected from the network?"
                )
                raise SystemExit(1)
            return [*env_args, "--add-host", f"{self.container_name}:{sidecar_ip}"]

        if not agent_network:
            return [*env_args, "--network", self.network_name]
        return [*env_args]

    # --- Public: release ---

    def release(self) -> None:
        # Runs under the runner's shared lock, only after its SidecarTracker reported
        # no other live agent on this container. Idempotent: a no-op when not running.
        if not self._is_running():
            return
        # Recover the key BEFORE stopping: `--rm` destroys the env source.
        try:
            self._master_key = self._recover_master_key()
        except SystemExit:
            self._master_key = ""
        if self._master_key:
            self.config.on_stopping(self._master_key)
        self._display.spin_while(
            label=self._label,
            message="stopping…",
            done_message="stopped",
            work=lambda: docker_run("stop", self.container_name),
        )

    # --- Internal helpers ---

    def _config_path(self) -> Path:
        """Return the resolved config.yaml path, validating it exists."""
        config = self.config.config_path
        if not config.exists():
            self._display.error(f"{self._label}: config not found at {config}")
            raise SystemExit(1)
        return config

    def _callback_dir(self) -> Path:
        return self.config.callback_dir

    def _log_dir(self) -> Path:
        return self.config.log_dir

    def _generate_master_key(self) -> str:
        uid = generate_uuid()
        return f"{self.config.master_key_prefix}{uid.replace('-', '')}"

    def _recover_master_key(self) -> str:
        stdout, rc = docker_run(
            "inspect",
            self.container_name,
            "--format={{range .Config.Env}}{{println .}}{{end}}",
        )
        if rc != 0:
            msg = (
                f"{self._label}: LITELLM_MASTER_KEY not recoverable from "
                f"{self.container_name} (container gone); aborting"
            )
            raise SystemExit(msg)
        for line in stdout.splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                key = line.removeprefix("LITELLM_MASTER_KEY=")
                if key:
                    return key
        msg = (
            f"{self._label}: LITELLM_MASTER_KEY not recoverable from "
            f"{self.container_name} (env line absent); aborting"
        )
        raise SystemExit(msg)

    def _recover_port(self) -> int:
        """
        Read the port a running container recorded. An unrecoverable value is fatal.

        Guessing the preferred base instead would misroute rather than fail: every
        provider shares one base, and in host-network mode a container port *is* a host
        port, so the base is most probably a *different* provider's sidecar. The agent
        would then connect successfully to the wrong upstream, with the wrong credentials
        and the wrong log subtree. So this is as fatal as a missing master key — a
        container that predates this env var, or that someone started by hand, must be
        removed so the next launch does a cold start.
        """
        stdout, rc = docker_run(
            "inspect",
            self.container_name,
            "--format={{range .Config.Env}}{{println .}}{{end}}",
        )
        if rc != 0:
            self._display.error(self._port_recovery_error("container gone"))
            raise SystemExit(1)
        prefix = f"{SIDECAR_PORT_ENV}="
        for line in stdout.splitlines():
            if line.startswith(prefix):
                raw = line.removeprefix(prefix).strip()
                if raw.isdigit():
                    return int(raw)
                self._display.error(self._port_recovery_error(f"unparseable value {raw!r}"))
                raise SystemExit(1)
        self._display.error(self._port_recovery_error("env line absent"))
        raise SystemExit(1)

    def _port_recovery_error(self, reason: str) -> str:
        """Build the abort message for an unrecoverable recorded port."""
        return (
            f"{self._label}: {SIDECAR_PORT_ENV} not recoverable from "
            f"{self.container_name} ({reason}); aborting\n"
            f"Remove it with `docker rm -f {self.container_name}` "
            "so the next launch cold-starts."
        )

    def _is_running(self) -> bool:
        stdout, rc = docker_run(
            "container",
            "inspect",
            "-f",
            "{{.State.Running}}",
            self.container_name,
        )
        return rc == 0 and stdout.strip() == "true"

    def _is_on_network(self, network: str) -> bool:
        stdout, rc = docker_run(
            "inspect",
            self.container_name,
            "--format",
            "{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}",
        )
        if rc != 0:
            return False
        return network in stdout.splitlines()

    def _ensure_network(self) -> None:
        if network_exists(self.network_name):
            return
        _, rc = docker_run("network", "create", self.network_name)
        if rc != 0:
            self._display.error(
                f"{self._label}: failed to create docker network {self.network_name}"
            )
            raise SystemExit(1)

    def _ensure_image(self) -> None:
        """Pull the sidecar image if it isn't present locally (streams progress)."""
        if image_exists(self.image):
            return
        self._display.warning(
            f"{self._label}: pulling {self.image} (first run, may take a few minutes)…"
        )
        _, rc = docker_run("pull", self.image, capture=False, timeout=900)
        if rc != 0:
            self._display.error(f"{self._label}: failed to pull image {self.image}")
            raise SystemExit(1)

    def _start(self, secrets: dict[str, str], master_key: str, sidecar_mode: str) -> None:
        config_path = self._config_path()
        callback_dir = self._callback_dir()
        log_dir = self._log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)

        # Reap any stopped container under our name
        _, rc = docker_run("container", "inspect", self.container_name)
        if rc == 0:
            docker_run("rm", "-f", self.container_name)

        network = "host" if sidecar_mode == "host" else self.network_name

        # 127.0.0.1 is the container's own loopback in bridge mode and the host's in
        # host mode — correct either way, as long as the port matches what the proxy
        # was told to bind (self.port, resolved before this call).
        health_cmd = (
            f'python3 -c "import urllib.request; '
            f"urllib.request.urlopen("
            f"'http://127.0.0.1:{self.port}{self.health_endpoint}', "
            f'timeout=2).read()"'
        )

        # Passed through keyed by the names the provider declared in required_secrets(),
        # so a provider may declare none (local model) or several (multi-upstream).
        sidecar_env = self.config.get_sidecar_env(secrets)
        env_flags: list[str] = []
        for key, value in sidecar_env.items():
            env_flags.extend(["-e", f"{key}={value}"])
        env_flags.extend(["-e", f"LITELLM_MASTER_KEY={master_key}"])
        # The provider is fixed for this sidecar's lifetime (one container per
        # provider), so the callback reads it from the container env, not per-request.
        env_flags.extend(["-e", f"{SIDECAR_PROVIDER_ENV}={self.config.provider_name}"])
        # Recorded so later launches adopt this port instead of re-scanning.
        env_flags.extend(["-e", f"{SIDECAR_PORT_ENV}={self.port}"])

        cmd = [
            "run",
            "-d",
            "--rm",
            "--name",
            self.container_name,
            "--network",
            network,
            *get_user_args(),
            "--health-cmd",
            health_cmd,
            "--health-interval=30s",
            "--health-retries=3",
            "--health-timeout=2s",
            f"--health-start-period={self.health_timeout_sec}s",
            "--health-start-interval=100ms",
            *env_flags,
            "-v",
            f"{config_path}:/etc/litellm/config.yaml:ro",
            # Mount every .py file from the litellm_runtime/ directory. LiteLLM
            # resolves the callback relative to the config file's directory, so
            # these must sit beside config.yaml at /etc/litellm/. See callback.py.
            *chain(
                *(
                    (
                        ("-v", f"{f}:/etc/litellm/{f.name}:ro")
                        for f in callback_dir.iterdir()
                        if f.suffix == ".py"
                    )
                    if callback_dir.is_dir()
                    else ()
                )
            ),
            "-v",
            f"{log_dir}:/var/log/agent-wrap",
            self.image,
            "--config",
            "/etc/litellm/config.yaml",
            "--port",
            str(self.port),
        ]
        _, rc = docker_run(*cmd)
        if rc != 0:
            self._display.error(f"{self._label}: failed to start {self.container_name}")
            raise SystemExit(1)

    def _health_poll(self) -> bool:
        def poll() -> tuple[PollResult, str]:
            stdout, rc = docker_run(
                "inspect",
                self.container_name,
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
            label=self._label,
            poll=poll,
            message="waiting for healthy",
            done_message="ready",
            timeout=self.health_timeout_sec,
        )

    def _attach_to_network(self, network: str) -> None:
        _, rc = docker_run("network", "inspect", network)
        if rc != 0:
            self._display.error(
                f"{self._label}: network '{network}' (from agent-run-args) does not exist"
            )
            raise SystemExit(1)

        # Check if already connected
        if self._is_on_network(network):
            return

        _, rc = docker_run("network", "connect", network, self.container_name)
        if rc != 0:
            self._display.error(
                f"{self._label}: failed to attach {self.container_name} to network '{network}'"
            )
            raise SystemExit(1)

    def _sidecar_ip_on_network(self, network: str) -> str:
        fmt = (
            f'{{{{with index .NetworkSettings.Networks "{network}"}}}}{{{{.IPAddress}}}}{{{{end}}}}'
        )
        stdout, rc = docker_run(
            "inspect",
            self.container_name,
            "--format",
            fmt,
        )
        return stdout.strip() if rc == 0 else ""
