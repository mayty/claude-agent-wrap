# This file has been created with the assistance of an AI tool.
"""Shared LiteLLM sidecar provider base.

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
from pathlib import Path
from typing import ClassVar, IO

from agent_wrap.providers.base import Provider
from agent_wrap.utils import generate_uuid


def _docker(*args: str, capture: bool = True, check: bool = False) -> tuple[str, int]:
    """Run a docker command and return (stdout, returncode)."""
    import subprocess

    try:
        result = subprocess.run(
            ["docker", *args],
            capture_output=capture,
            text=True,
            timeout=30,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr}")
        return result.stdout.strip(), result.returncode
    except subprocess.TimeoutExpired:
        return "", 1
    except FileNotFoundError:
        return "", 1


class LiteLLMProvider(Provider):
    """Base class for LiteLLM-backed providers.

    Manages a shared sidecar container that fronts the model API. Subclasses
    specify which API (Bedrock, Dashscope, etc.) by overriding class attributes
    and two abstract methods.
    """

    # --- Class attributes (overridden by subclasses) ---

    #: Pinned Docker image with tag + digest.
    image: ClassVar[str] = ""
    #: Name of the lock file under .agent-launches/.
    lock_file: ClassVar[str] = "litellm.lock"
    #: Name of the refcount file under .agent-launches/.
    refcount_file: ClassVar[str] = "litellm.refcount"
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
            raise SystemExit(f"litellm-sidecar: config not found at {config}")
        return config

    # --- Public: ensure ---

    def ensure(
        self,
        tool_dir: Path,
        use_host_net: bool,
        instance_id: str,
        agent_network: str | None,
    ) -> None:
        state_dir = tool_dir / ".agent-launches"
        state_dir.mkdir(parents=True, exist_ok=True)

        lock_path = state_dir / self.lock_file
        self._lock_file = open(lock_path, "w")
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
            raise SystemExit(f"litellm-sidecar: timed out waiting for lock {lock_path}")

        try:
            self._ensure_network()

            if agent_network == "bridge":
                raise SystemExit(
                    "litellm-sidecar: --network bridge is not supported "
                    "(Docker's default bridge has no embedded DNS).\n"
                    "  Use a user-defined network (`docker network create <name>`) "
                    "or remove --network from agent-run-args to use agent-wrap-net."
                )

            agent_in_host_netns = bool(use_host_net) or agent_network == "host"

            # Migration: sidecar from before agent-wrap-net refactor
            if (
                self._is_running()
                and not self._is_on_network(self.network_name)
                and not self._is_on_network("host")
            ):
                print("litellm-sidecar: existing sidecar predates agent-wrap-net; restarting", file=sys.stderr)
                _docker("stop", self.container_name)

            if self._is_running():
                # First-launch-wins: inherit running mode
                sidecar_mode = "host" if self._is_on_network("host") else "bridge"
                self._master_key = self._recover_master_key()
            else:
                sidecar_mode = "host" if use_host_net else "bridge"
                secret_key = self.read_secret_key(self._load_secrets())
                self._master_key = self._generate_master_key()
                self._start(secret_key, self._master_key, sidecar_mode)
                if not self._health_poll():
                    print("litellm-sidecar: health check failed; recent logs:", file=sys.stderr)
                    _docker("logs", "--tail", "50", self.container_name)
                    _docker("stop", self.container_name)
                    raise SystemExit(1)

            # Attach sidecar to agent's custom network if needed
            if (
                sidecar_mode != "host"
                and agent_network
                and agent_network not in ("host", "none", self.network_name)
            ):
                self._attach_to_network(agent_network)

            self._register_instance(tool_dir, instance_id)

            # Build agent-side env vars
            base_url = f"http://{self.container_name}:{self.internal_port}"
            agent_env = self.get_agent_env(self._master_key, base_url)
            env_args: list[str] = []
            for key, value in agent_env.items():
                env_args.extend(["-e", f"{key}={value}"])

            # Connectivity matrix
            if sidecar_mode == "host":
                if agent_in_host_netns:
                    self._run_args = [*env_args, "--add-host", f"{self.container_name}:127.0.0.1"]
                else:
                    self._run_args = [*env_args, "--add-host", f"{self.container_name}:host-gateway"]
            elif agent_in_host_netns:
                sidecar_ip = self._sidecar_ip_on_network(self.network_name)
                if not sidecar_ip:
                    raise SystemExit(
                        f"litellm-sidecar: sidecar has no IP on {self.network_name} "
                        "— was it disconnected from the network?"
                    )
                self._run_args = [*env_args, "--add-host", f"{self.container_name}:{sidecar_ip}"]
            elif not agent_network:
                self._run_args = [*env_args, "--network", self.network_name]
            else:
                self._run_args = [*env_args]
        finally:
            if self._lock_file is not None:
                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)

    # --- Public: release ---

    def release(self, tool_dir: Path, instance_id: str) -> None:
        if not instance_id:
            return

        lock_path = tool_dir / ".agent-launches" / self.lock_file
        if not lock_path.exists():
            return

        lock_file = open(lock_path, "w")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError:
            return

        try:
            self._unregister_instance(tool_dir, instance_id)
            self._reconcile_refcount(tool_dir)

            if not self._has_active_instances(tool_dir) and self._is_running():
                _docker("stop", self.container_name)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    # --- Public: run/label args ---

    def get_run_args(self) -> list[str]:
        return list(self._run_args)

    def get_label_args(self, instance_id: str) -> list[str]:
        if not instance_id:
            return []
        return [
            "--label", "agent-wrap.role=claude-agent",
            "--label", f"agent-wrap.instance-id={instance_id}",
            "--name", f"claude-agent-{instance_id}",
        ]

    # --- Internal helpers ---

    def _generate_master_key(self) -> str:
        uid = generate_uuid()
        return f"{self.master_key_prefix}{uid.replace('-', '')}"

    def _load_secrets(self) -> dict:
        secrets_path = Path.home() / "claude_keys.json"
        if not secrets_path.exists():
            raise SystemExit(f"litellm-sidecar: {secrets_path} not found")
        try:
            return json.loads(secrets_path.read_text())
        except json.JSONDecodeError:
            raise SystemExit(f"litellm-sidecar: {secrets_path} is not valid JSON")

    def _recover_master_key(self) -> str:
        stdout, rc = _docker(
            "inspect", self.container_name,
            "--format={{range .Config.Env}}{{println .}}{{end}}",
        )
        if rc != 0:
            raise SystemExit(
                f"litellm-sidecar: LITELLM_MASTER_KEY not recoverable from "
                f"{self.container_name} (container gone); aborting"
            )
        for line in stdout.splitlines():
            if line.startswith("LITELLM_MASTER_KEY="):
                key = line.removeprefix("LITELLM_MASTER_KEY=")
                if key:
                    return key
        raise SystemExit(
            f"litellm-sidecar: LITELLM_MASTER_KEY not recoverable from "
            f"{self.container_name} (env line absent); aborting"
        )

    def _is_running(self) -> bool:
        stdout, rc = _docker(
            "container", "inspect",
            "-f", "{{.State.Running}}",
            self.container_name,
        )
        return rc == 0 and stdout.strip() == "true"

    def _is_on_network(self, network: str) -> bool:
        stdout, rc = _docker(
            "inspect", self.container_name,
            "--format", "{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}",
        )
        if rc != 0:
            return False
        return network in stdout.splitlines()

    def _ensure_network(self) -> None:
        _, rc = _docker("network", "inspect", self.network_name)
        if rc == 0:
            return
        _, rc = _docker("network", "create", self.network_name)
        if rc != 0:
            raise SystemExit(f"litellm-sidecar: failed to create docker network {self.network_name}")

    def _start(self, secret_key: str, master_key: str, sidecar_mode: str) -> None:
        config_path = self._config_path()

        # Reap any stopped container under our name
        _, rc = _docker("container", "inspect", self.container_name)
        if rc == 0:
            _docker("rm", "-f", self.container_name)

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
            "run", "-d", "--rm",
            "--name", self.container_name,
            "--network", network,
            "--health-cmd", health_cmd,
            "--health-interval=30s",
            "--health-retries=3",
            "--health-timeout=2s",
            f"--health-start-period={self.health_timeout_sec}s",
            "--health-start-interval=100ms",
            *env_flags,
            "-v", f"{config_path}:/etc/litellm/config.yaml:ro",
            self.image,
            "--config", "/etc/litellm/config.yaml",
            "--port", str(self.internal_port),
            *self.get_sidecar_cmd_args(),
        ]
        _, rc = _docker(*cmd)
        if rc != 0:
            raise SystemExit(f"litellm-sidecar: failed to start {self.container_name}")

    def _health_poll(self) -> bool:
        deadline = time.monotonic() + self.health_timeout_sec
        is_tty = sys.stderr.isatty()
        spinner = ["|", "/", "-", "\\"]
        frame = 0
        last_status = ""
        start = time.monotonic()

        while time.monotonic() < deadline:
            stdout, rc = _docker(
                "inspect", self.container_name,
                "--format={{.State.Health.Status}}",
            )
            if rc != 0:
                self._health_end(is_tty, False, time.monotonic() - start)
                return False

            status = stdout.strip()

            if is_tty:
                elapsed = int(time.monotonic() - start)
                print(
                    f"\r\033[2Klitellm-sidecar: {spinner[frame]} waiting for "
                    f"healthy [{status or '?'}] ({elapsed}s)",
                    end="", file=sys.stderr,
                )
                frame = (frame + 1) % len(spinner)
            elif status and status != last_status:
                print(f"litellm-sidecar: {status}", file=sys.stderr)
                last_status = status

            if status == "healthy":
                self._health_end(is_tty, True, time.monotonic() - start)
                return True
            if status == "unhealthy" or not self._is_running():
                self._health_end(is_tty, False, time.monotonic() - start)
                return False

            time.sleep(0.5)

        self._health_end(is_tty, False, time.monotonic() - start)
        return False

    @staticmethod
    def _health_end(is_tty: bool, success: bool, elapsed: float) -> None:
        if is_tty:
            if success:
                print(f"\r\033[2Klitellm-sidecar: ready ({int(elapsed)}s)", file=sys.stderr)
            else:
                print(file=sys.stderr)

    def _attach_to_network(self, network: str) -> None:
        _, rc = _docker("network", "inspect", network)
        if rc != 0:
            raise SystemExit(
                f"litellm-sidecar: network '{network}' (from agent-run-args) does not exist"
            )

        # Check if already connected
        if self._is_on_network(network):
            return

        _, rc = _docker("network", "connect", network, self.container_name)
        if rc != 0:
            raise SystemExit(
                f"litellm-sidecar: failed to attach {self.container_name} to network '{network}'"
            )

    def _sidecar_ip_on_network(self, network: str) -> str:
        stdout, rc = _docker(
            "inspect", self.container_name,
            "--format",
            f'{{{{with index .NetworkSettings.Networks "{network}"}}}}{{{{.IPAddress}}}}{{{{end}}}}',
        )
        return stdout.strip() if rc == 0 else ""

    def _refcount_path(self, tool_dir: Path) -> Path:
        return tool_dir / ".agent-launches" / self.refcount_file

    def _register_instance(self, tool_dir: Path, instance_id: str) -> None:
        path = self._refcount_path(tool_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text().splitlines() if path.exists() else []
        if instance_id not in existing:
            with open(path, "a") as f:
                f.write(instance_id + "\n")

    def _unregister_instance(self, tool_dir: Path, instance_id: str) -> None:
        path = self._refcount_path(tool_dir)
        if not path.exists():
            return
        lines = [l for l in path.read_text().splitlines() if l != instance_id]
        path.write_text("\n".join(lines) + "\n" if lines else "")

    def _has_active_instances(self, tool_dir: Path) -> bool:
        path = self._refcount_path(tool_dir)
        if not path.exists():
            return False
        return any(line.strip() for line in path.read_text().splitlines())

    def _reconcile_refcount(self, tool_dir: Path) -> None:
        """Drop refcount entries whose agent container no longer exists."""
        path = self._refcount_path(tool_dir)
        if not path.exists():
            return
        entries = [l for l in path.read_text().splitlines() if l.strip()]
        if not entries:
            return

        stdout, rc = _docker(
            "ps",
            "--filter", "label=agent-wrap.role=claude-agent",
            "--format", '{{.Label "agent-wrap.instance-id"}}',
        )
        if rc != 0:
            return

        live = set(stdout.splitlines())
        kept = [e for e in entries if e in live]
        path.write_text("\n".join(kept) + "\n" if kept else "")
