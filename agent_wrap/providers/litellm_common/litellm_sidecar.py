# This file has been created with the assistance of an AI tool.
"""
The LiteLLM proxy as a ``Sidecar``.

Implements the full sidecar lifecycle: lazy start, health polling, an activity
heartbeat (live count comes from ``docker ps``), network-mode detection, master key
minting/recovery, and the connectivity matrix for agent↔sidecar communication across
network namespaces.

``LiteLLMSidecar`` is a pure mechanism configured by an immutable
``LiteLLMSidecarConfig`` — the owning provider supplies the image pin, resolved
on-disk paths, and the provider-specific behavior hooks (upstream auth env, agent
env, key approval). It holds no back-reference to the provider, so it is unit-testable
on its own. Locking and the start/stop decision are the runner's concern (one shared
lock + one ``SidecarTracker``); this class only ensures/stops its container.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_wrap.lib.docker_utils import docker_run, get_user_args, image_exists
from agent_wrap.lib.spinner import PollResult, Spinner
from agent_wrap.lib.utils import generate_uuid, project_path_hash
from agent_wrap.sidecars.base import Sidecar

if TYPE_CHECKING:
    from collections.abc import Callable


_SPINNER = Spinner("litellm-sidecar")


@dataclass(frozen=True)
class LiteLLMSidecarConfig:
    """Immutable configuration for a ``LiteLLMSidecar``, built by the provider."""

    # --- identity ---
    image: str
    container_name: str
    network_name: str
    internal_port: int
    master_key_prefix: str
    #: Provider name, passed to the sidecar as AGENT_WRAP_PROVIDER for log routing.
    provider_name: str

    # --- health / concurrency timing ---
    health_timeout_sec: int
    health_endpoint: str
    cold_start_time: float
    short_circuit_time: float

    # --- resolved paths (provider resolves these; introspecting the subclass) ---
    config_path: Path
    callback_dir: Path
    log_dir: Path

    # --- behavior hooks (provider-specific) ---
    get_sidecar_env: Callable[[dict], dict[str, str]]
    get_agent_env: Callable[[str, str], dict[str, str]]
    read_secret_key: Callable[[dict], str]
    get_sidecar_cmd_args: Callable[[], list[str]]
    on_started: Callable[[str], None]
    on_stopping: Callable[[str], None]


class LiteLLMSidecar(Sidecar):
    """The shared LiteLLM proxy container, managed as a singleton sidecar."""

    def __init__(self, config: LiteLLMSidecarConfig) -> None:
        self.config = config
        self._master_key: str = ""

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
        return self.config.internal_port

    @property
    def image(self) -> str:
        return self.config.image

    @property
    def health_timeout_sec(self) -> int:
        return self.config.health_timeout_sec

    @property
    def health_endpoint(self) -> str:
        return self.config.health_endpoint

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
    ) -> list[str]:
        if agent_network == "bridge":
            msg = (
                "litellm-sidecar: --network bridge is not supported "
                "(Docker's default bridge has no embedded DNS).\n"
                "  Use a user-defined network (`docker network create <name>`) "
                "or remove --network from agent-run-args to use agent-wrap-net."
            )
            raise SystemExit(msg)

        agent_in_host_netns = bool(use_host_net) or agent_network == "host"

        # Runs under the runner's shared lock (held across the whole launch), so the
        # start decision + health poll are atomic against every concurrent launcher.
        self._ensure_network()
        sidecar_mode = self._ensure_sidecar(use_host_net=use_host_net)

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

    def _ensure_sidecar(self, *, use_host_net: bool) -> str:
        """Ensure the sidecar is running + healthy. Returns its network mode."""
        # Migration: sidecar from before agent-wrap-net refactor
        if (
            self._is_running()
            and not self._is_on_network(self.network_name)
            and not self._is_on_network("host")
        ):
            print(
                "litellm-sidecar: existing sidecar predates agent-wrap-net; restarting",
                file=sys.stderr,
            )
            docker_run("stop", self.container_name)

        if self._is_running():
            # First-launch-wins: inherit running mode. The key was already approved
            # by whoever started the sidecar (on_started), so do not re-approve here.
            sidecar_mode = "host" if self._is_on_network("host") else "bridge"
            self._master_key = self._recover_master_key()
        else:
            sidecar_mode = "host" if use_host_net else "bridge"
            secret_key = self.config.read_secret_key(self._load_secrets())
            self._master_key = self._generate_master_key()
            self._start(secret_key, self._master_key, sidecar_mode)
            self.config.on_started(self._master_key)
            if not self._health_poll():
                # Leave the unhealthy container in place and raise: release() (which
                # always runs — see run.py) is the single home for the stop, and it
                # needs the container present to recover the key for un-approval. A
                # later cold start's _start() reaps it via `rm -f`.
                print("litellm-sidecar: health check failed; recent logs:", file=sys.stderr)
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
        base_url = f"http://{self.container_name}:{self.internal_port}"
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
                msg = (
                    f"litellm-sidecar: sidecar has no IP on {self.network_name} "
                    "— was it disconnected from the network?"
                )
                raise SystemExit(msg)
            return [*env_args, "--add-host", f"{self.container_name}:{sidecar_ip}"]

        if not agent_network:
            return [*env_args, "--network", self.network_name]
        return [*env_args]

    # --- Public: release ---

    def release(self) -> None:
        # Runs under the runner's shared lock, only after its SidecarTracker decided
        # the run may stop. Idempotent: a no-op when the container isn't running.
        if not self._is_running():
            return
        # Recover the key BEFORE stopping: `--rm` destroys the env source.
        try:
            self._master_key = self._recover_master_key()
        except SystemExit:
            self._master_key = ""
        if self._master_key:
            self.config.on_stopping(self._master_key)
        _SPINNER.spin_while(
            message="stopping…",
            done_message="stopped",
            work=lambda: docker_run("stop", self.container_name),
        )

    # --- Internal helpers ---

    def _config_path(self) -> Path:
        """Return the resolved config.yaml path, validating it exists."""
        config = self.config.config_path
        if not config.exists():
            msg = f"litellm-sidecar: config not found at {config}"
            raise SystemExit(msg)
        return config

    def _callback_dir(self) -> Path:
        return self.config.callback_dir

    def _log_dir(self) -> Path:
        return self.config.log_dir

    def _generate_master_key(self) -> str:
        uid = generate_uuid()
        return f"{self.config.master_key_prefix}{uid.replace('-', '')}"

    def _load_secrets(self) -> dict[str, Any]:
        secrets_path = Path.home() / "claude_keys.json"
        if not secrets_path.exists():
            msg = f"litellm-sidecar: {secrets_path} not found"
            raise SystemExit(msg)
        try:
            return json.loads(secrets_path.read_text())
        except json.JSONDecodeError:
            msg = f"litellm-sidecar: {secrets_path} is not valid JSON"
            raise SystemExit(msg) from None

    def _recover_master_key(self) -> str:
        stdout, rc = docker_run(
            "inspect",
            self.container_name,
            "--format={{range .Config.Env}}{{println .}}{{end}}",
        )
        if rc != 0:
            msg = (
                f"litellm-sidecar: LITELLM_MASTER_KEY not recoverable from "
                f"{self.container_name} (container gone); aborting"
            )
            raise SystemExit(msg)
        for line in stdout.splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                key = line.removeprefix("LITELLM_MASTER_KEY=")
                if key:
                    return key
        msg = (
            f"litellm-sidecar: LITELLM_MASTER_KEY not recoverable from "
            f"{self.container_name} (env line absent); aborting"
        )
        raise SystemExit(msg)

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
        _, rc = docker_run("network", "inspect", self.network_name)
        if rc == 0:
            return
        _, rc = docker_run("network", "create", self.network_name)
        if rc != 0:
            msg = f"litellm-sidecar: failed to create docker network {self.network_name}"
            raise SystemExit(msg)

    def _ensure_image(self) -> None:
        """Pull the sidecar image if it isn't present locally (streams progress)."""
        if image_exists(self.image):
            return
        print(
            f"litellm-sidecar: pulling {self.image} (first run, may take a few minutes)…",
            file=sys.stderr,
        )
        _, rc = docker_run("pull", self.image, capture=False, timeout=900)
        if rc != 0:
            msg = f"litellm-sidecar: failed to pull image {self.image}"
            raise SystemExit(msg)

    def _start(self, secret_key: str, master_key: str, sidecar_mode: str) -> None:
        config_path = self._config_path()
        callback_dir = self._callback_dir()
        log_dir = self._log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)

        # Reap any stopped container under our name
        _, rc = docker_run("container", "inspect", self.container_name)
        if rc == 0:
            docker_run("rm", "-f", self.container_name)

        network = "host" if sidecar_mode == "host" else self.network_name

        health_cmd = (
            f'python3 -c "import urllib.request; '
            f"urllib.request.urlopen("
            f"'http://127.0.0.1:{self.internal_port}{self.health_endpoint}', "
            f'timeout=2).read()"'
        )

        sidecar_env = self.config.get_sidecar_env({"_secret_key": secret_key})
        env_flags: list[str] = []
        for key, value in sidecar_env.items():
            env_flags.extend(["-e", f"{key}={value}"])
        env_flags.extend(["-e", f"LITELLM_MASTER_KEY={master_key}"])
        # The provider is fixed for the shared sidecar's lifetime (first-launch-wins),
        # so the callback reads it from the container env rather than per-request.
        env_flags.extend(["-e", f"AGENT_WRAP_PROVIDER={self.config.provider_name}"])

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
            # Logging callback (resolved by LiteLLM relative to the config file's
            # directory, so it must sit next to config.yaml) and the host log dir
            # it appends request/response JSONL to. See callback.py.
            *chain(
                *(
                    ("-v", f"{callback_dir / filename}:/etc/litellm/{filename}:ro")
                    for filename in ("callback.py", "string_hasher.py", "helpers.py")
                )
            ),
            "-v",
            f"{log_dir}:/var/log/agent-wrap",
            self.image,
            "--config",
            "/etc/litellm/config.yaml",
            "--port",
            str(self.internal_port),
            *self.config.get_sidecar_cmd_args(),
        ]
        _, rc = docker_run(*cmd)
        if rc != 0:
            msg = f"litellm-sidecar: failed to start {self.container_name}"
            raise SystemExit(msg)

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

        return _SPINNER.poll_until(
            poll=poll,
            message="waiting for healthy",
            done_message="ready",
            timeout=self.health_timeout_sec,
        )

    def _attach_to_network(self, network: str) -> None:
        _, rc = docker_run("network", "inspect", network)
        if rc != 0:
            msg = f"litellm-sidecar: network '{network}' (from agent-run-args) does not exist"
            raise SystemExit(msg)

        # Check if already connected
        if self._is_on_network(network):
            return

        _, rc = docker_run("network", "connect", network, self.container_name)
        if rc != 0:
            msg = f"litellm-sidecar: failed to attach {self.container_name} to network '{network}'"
            raise SystemExit(msg)

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
