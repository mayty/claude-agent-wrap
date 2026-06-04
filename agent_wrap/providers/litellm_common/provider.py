# This file has been created with the assistance of an AI tool.
"""
Shared LiteLLM sidecar provider base.

Implements the full sidecar lifecycle: lazy start, health polling, refcounting,
network-mode detection, master key minting/recovery, and the connectivity
matrix for agent-sidecar communication across network namespaces.

Subclasses only need to specify image pins, auth key paths, and agent-side
env vars — everything else is shared.
"""

from __future__ import annotations

import fcntl
import json
import sys
import time
from abc import abstractmethod
from itertools import chain
from pathlib import Path
from typing import IO, ClassVar

from agent_wrap.lib.console import Ansi
from agent_wrap.lib.docker_utils import docker_run, image_exists
from agent_wrap.lib.utils import generate_uuid
from agent_wrap.providers.base import Provider


class LiteLLMProvider(Provider):
    """
    Base class for LiteLLM-backed providers.

    Manages a shared sidecar container that fronts the model API. Subclasses
    specify which API (Bedrock, Dashscope, etc.) by overriding class attributes
    and two abstract methods.
    """

    # --- Class attributes (overridden by subclasses) ---

    #: Pinned Docker image with tag + digest.
    image: ClassVar[str] = ""
    #: Name of the lock file in the provider's source directory.
    lock_file: ClassVar[str] = "lock"
    #: Name of the refcount file in the provider's source directory.
    refcount_file: ClassVar[str] = "refcount"
    #: Prefix for generated master keys (e.g. "sk-aw-" for bedrock).
    master_key_prefix: ClassVar[str] = "sk-aw-"

    # --- Shared defaults (rarely overridden) ---

    container_name: ClassVar[str] = "agent-wrap-litellm"
    network_name: ClassVar[str] = "agent-wrap-net"
    internal_port: ClassVar[int] = 4000
    health_timeout_sec: ClassVar[int] = 90
    lock_timeout_sec: ClassVar[int] = 120
    health_endpoint: ClassVar[str] = "/health/liveliness"

    def __init__(self) -> None:
        self._master_key: str = ""
        self._run_args: list[str] = []
        self._lock_file: IO | None = None

    # --- Abstract hooks (subclasses must implement) ---

    @abstractmethod
    def get_sidecar_env(self, secrets: dict) -> dict[str, str]:
        """Return env vars for the sidecar container (upstream auth tokens)."""

    @abstractmethod
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        """Return env vars injected into the agent container."""

    @abstractmethod
    def read_secret_key(self, secrets: dict) -> str:
        """Extract the upstream API key from parsed secrets."""

    @abstractmethod
    def get_sidecar_cmd_args(self) -> list[str]:
        """Return extra command-line args for the sidecar container entrypoint."""

    # --- Config resolution ---

    def _config_path(self) -> Path:
        """Resolve config.yaml next to this provider's provider.py."""
        # Walk up to the provider directory (parent of the submodule dir)
        provider_dir = Path(__file__).parent.parent / self.__class__.__module__.split(".")[-2]
        config = provider_dir / "config.yaml"
        if not config.exists():
            msg = f"litellm-sidecar: config not found at {config}"
            raise SystemExit(msg)
        return config

    def _state_dir(self) -> Path:
        """Resolve the provider's source directory (for lock/refcount files)."""
        return Path(__file__).parent.parent / self.__class__.__module__.split(".")[-2]

    def _callback_dir(self) -> Path:
        """Resolve the shared LiteLLM logging callback (mounted into the sidecar)."""
        return Path(__file__).parent

    def _log_dir(self) -> Path:
        """
        Host directory for the request/response JSONL log (bind-mounted into sidecar).

        Includes the provider name (e.g., 'litellm-bedrock') so the sidecar mounts
        directly to /var/log/agent-wrap, letting the callback simply write to
        /var/log/agent-wrap/<session_id>/messages.jsonl.
        """
        provider_name = self.__class__.name if hasattr(self.__class__, "name") else "unknown"
        return Path.cwd() / ".claude" / "litellm-logs" / provider_name

    # --- Public: ensure ---

    def ensure(
        self,
        *,
        use_host_net: bool,
        instance_id: str,
        agent_network: str | None,
    ) -> None:
        self._acquire_lock()

        try:
            self._ensure_network()

            if agent_network == "bridge":
                msg = (
                    "litellm-sidecar: --network bridge is not supported "
                    "(Docker's default bridge has no embedded DNS).\n"
                    "  Use a user-defined network (`docker network create <name>`) "
                    "or remove --network from agent-run-args to use agent-wrap-net."
                )
                raise SystemExit(msg)

            agent_in_host_netns = bool(use_host_net) or agent_network == "host"

            sidecar_mode = self._ensure_sidecar(use_host_net=use_host_net)

            # Attach sidecar to agent's custom network if needed
            if (
                sidecar_mode != "host"
                and agent_network
                and agent_network not in ("host", "none", self.network_name)
            ):
                self._attach_to_network(agent_network)

            self._register_instance(instance_id)
            self._run_args = self._build_connectivity_args(
                sidecar_mode, agent_in_host_netns=agent_in_host_netns, agent_network=agent_network
            )
        finally:
            if self._lock_file is not None:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)

    def _acquire_lock(self) -> None:
        """Acquire the flock on the lock file, waiting up to lock_timeout_sec."""
        lock_path = self._state_dir() / self.lock_file
        self._lock_file = open(lock_path, "w")  # noqa: SIM115
        deadline = time.monotonic() + self.lock_timeout_sec
        acquired = False
        while time.monotonic() < deadline:
            try:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(0.1)
        if not acquired:
            msg = f"litellm-sidecar: timed out waiting for lock {lock_path}"
            raise SystemExit(msg)

    def _ensure_sidecar(self, *, use_host_net: bool) -> str:
        """Ensure the sidecar is running. Returns the sidecar's network mode."""
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
            # First-launch-wins: inherit running mode
            sidecar_mode = "host" if self._is_on_network("host") else "bridge"
            self._master_key = self._recover_master_key()
        else:
            sidecar_mode = "host" if use_host_net else "bridge"
            secret_key = self.read_secret_key(self._load_secrets())
            self._master_key = self._generate_master_key()
            self._ensure_image()
            self._start(secret_key, self._master_key, sidecar_mode)
            if not self._health_poll():
                print("litellm-sidecar: health check failed; recent logs:", file=sys.stderr)
                docker_run("logs", "--tail", "50", self.container_name)
                docker_run("stop", self.container_name)
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
        agent_env = self.get_agent_env(self._master_key, base_url)
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

    def release(self, instance_id: str) -> None:
        if not instance_id:
            return

        lock_path = self._state_dir() / self.lock_file
        if not lock_path.exists():
            return

        lock_file = open(lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError:
            return

        try:
            self._unregister_instance(instance_id)
            self._reconcile_refcount()

            if not self._has_active_instances() and self._is_running():
                docker_run("stop", self.container_name)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # --- Public: run/label args ---

    def get_run_args(self) -> list[str]:
        return list(self._run_args)

    def get_label_args(self, instance_id: str) -> list[str]:
        if not instance_id:
            return []
        return [
            "--label",
            "agent-wrap.role=claude-agent",
            "--label",
            f"agent-wrap.instance-id={instance_id}",
            "--name",
            f"claude-agent-{instance_id}",
        ]

    # --- Internal helpers ---

    def _generate_master_key(self) -> str:
        uid = generate_uuid()
        return f"{self.master_key_prefix}{uid.replace('-', '')}"

    def _load_secrets(self) -> dict:
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

        sidecar_env = self.get_sidecar_env({"_secret_key": secret_key})
        env_flags: list[str] = []
        for key, value in sidecar_env.items():
            env_flags.extend(["-e", f"{key}={value}"])
        env_flags.extend(["-e", f"LITELLM_MASTER_KEY={master_key}"])

        cmd = [
            "run",
            "-d",
            "--rm",
            "--name",
            self.container_name,
            "--network",
            network,
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
                    for filename in ("callback.py", "string_hasher.py")
                )
            ),
            "-v",
            f"{log_dir}:/var/log/agent-wrap",
            self.image,
            "--config",
            "/etc/litellm/config.yaml",
            "--port",
            str(self.internal_port),
            *self.get_sidecar_cmd_args(),
        ]
        _, rc = docker_run(*cmd)
        if rc != 0:
            msg = f"litellm-sidecar: failed to start {self.container_name}"
            raise SystemExit(msg)

    def _health_poll(self) -> bool:
        deadline = time.monotonic() + self.health_timeout_sec
        is_tty = sys.stderr.isatty()
        spinner = ["|", "/", "-", "\\"]
        frame = 0
        last_status = ""
        start = time.monotonic()

        while time.monotonic() < deadline:
            stdout, rc = docker_run(
                "inspect",
                self.container_name,
                "--format={{.State.Health.Status}}",
            )
            if rc != 0:
                self._health_end(is_tty=is_tty, success=False, elapsed=time.monotonic() - start)
                return False

            status = stdout.strip()

            if is_tty:
                elapsed = int(time.monotonic() - start)
                print(
                    f"{Ansi.CR}{Ansi.ERASE_LINE}litellm-sidecar: {spinner[frame]} waiting for "
                    f"healthy [{status or '?'}] ({elapsed}s)",
                    end="",
                    file=sys.stderr,
                )
                frame = (frame + 1) % len(spinner)
            elif status and status != last_status:
                print(f"litellm-sidecar: {status}", file=sys.stderr)
                last_status = status

            if status == "healthy":
                self._health_end(is_tty=is_tty, success=True, elapsed=time.monotonic() - start)
                return True
            if status == "unhealthy" or not self._is_running():
                self._health_end(is_tty=is_tty, success=False, elapsed=time.monotonic() - start)
                return False

            time.sleep(0.5)

        self._health_end(is_tty=is_tty, success=False, elapsed=time.monotonic() - start)
        return False

    @staticmethod
    def _health_end(*, is_tty: bool, success: bool, elapsed: float) -> None:
        if is_tty:
            if success:
                print(
                    f"{Ansi.CR}{Ansi.ERASE_LINE}litellm-sidecar: ready ({int(elapsed)}s)",
                    file=sys.stderr,
                )
            else:
                print(file=sys.stderr)

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

    def _refcount_path(self) -> Path:
        return self._state_dir() / self.refcount_file

    def _register_instance(self, instance_id: str) -> None:
        path = self._refcount_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text().splitlines() if path.exists() else []
        if instance_id not in existing:
            with open(path, "a") as f:
                f.write(instance_id + "\n")

    def _unregister_instance(self, instance_id: str) -> None:
        path = self._refcount_path()
        if not path.exists():
            return
        lines = [ln for ln in path.read_text().splitlines() if ln != instance_id]
        path.write_text("\n".join(lines) + "\n" if lines else "")

    def _has_active_instances(self) -> bool:
        path = self._refcount_path()
        if not path.exists():
            return False
        return any(line.strip() for line in path.read_text().splitlines())

    def _reconcile_refcount(self) -> None:
        """Drop refcount entries whose agent container no longer exists."""
        path = self._refcount_path()
        if not path.exists():
            return
        entries = [ln for ln in path.read_text().splitlines() if ln.strip()]
        if not entries:
            return

        stdout, rc = docker_run(
            "ps",
            "--filter",
            "label=agent-wrap.role=claude-agent",
            "--format",
            '{{.Label "agent-wrap.instance-id"}}',
        )
        if rc != 0:
            return

        live = set(stdout.splitlines())
        kept = [e for e in entries if e in live]
        path.write_text("\n".join(kept) + "\n" if kept else "")
