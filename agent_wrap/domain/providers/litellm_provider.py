# This file has been created with the assistance of an AI tool.
"""
Shared LiteLLM sidecar provider base.

A LiteLLM-backed provider is a thin factory: it declares one ``LiteLLMSidecar``
(the shared proxy container) plus its pricing table. The container lifecycle —
lazy start, health polling, the activity heartbeat, network-mode detection, and
master key minting/recovery — lives in ``litellm_sidecar.py``; the provider only
supplies the image pin, auth-key paths, agent-side env vars, and resolved on-disk
paths.

Subclasses override a handful of class attributes and abstract hooks; the provider
wires them into a ``LiteLLMSidecarConfig`` in ``sidecars()``.
"""

from __future__ import annotations

from abc import abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agent_wrap.constants import LITELLM_IMAGE, TOOL_DIR
from agent_wrap.domain.providers.base import Provider

if TYPE_CHECKING:
    from agent_wrap.domain.sidecars.base import Sidecar


class LiteLLMProvider(Provider):
    """
    Base class for LiteLLM-backed providers.

    Declares a shared sidecar container that fronts the model API. Subclasses
    specify which API (Bedrock, Dashscope, etc.) by overriding class attributes
    and a few abstract hooks.
    """

    # --- Class attributes (overridden by subclasses) ---

    #: Pinned Docker image with tag + digest.
    image: ClassVar[str] = LITELLM_IMAGE
    #: Prefix for generated master keys (e.g. "sk-aw-" for bedrock).
    master_key_prefix: ClassVar[str] = "sk-aw-"
    #: Human-readable description of the API key this provider needs.
    #: Subclasses override this; the key name ``"api_key"`` is fixed.
    secret_description: ClassVar[str] = ""

    # --- Shared defaults (rarely overridden) ---

    container_name: ClassVar[str] = "agent-wrap-litellm"
    network_name: ClassVar[str] = "agent-wrap-net"
    internal_port: ClassVar[int] = 4000
    health_timeout_sec: ClassVar[int] = 90
    health_endpoint: ClassVar[str] = "/health/liveliness"
    #: Seconds a cold start takes (docker run + health poll). The one launcher that
    #: wins the shared lock pays this; it dominates the lock-timeout budget. Kept
    #: above health_timeout_sec for the docker-run + reap tail.
    cold_start_time: ClassVar[float] = 120.0
    #: Seconds one agent takes to walk the lock on the hot path (sidecar already up:
    #: recover key + connectivity). Sub-second in practice; the runner multiplies it
    #: by the expected queue depth to size the lock timeout.
    short_circuit_time: ClassVar[float] = 2.0

    # --- Sidecar declaration ---

    def sidecars(self) -> list[Sidecar]:
        """Return the LiteLLM proxy sidecar this provider depends on."""
        assert self._sidecar_service is not None  # set by ProviderService
        return [self._sidecar_service.create_litellm_sidecar(**self._sidecar_config())]

    def _sidecar_config(self) -> dict[str, object]:
        """Build the sidecar config kwargs, closing over this provider's hooks and paths."""
        return {
            "image": self.image,
            "container_name": self.container_name,
            "network_name": self.network_name,
            "internal_port": self.internal_port,
            "master_key_prefix": self.master_key_prefix,
            "provider_name": getattr(self.__class__, "name", "unknown"),
            "health_timeout_sec": self.health_timeout_sec,
            "health_endpoint": self.health_endpoint,
            "cold_start_time": float(self.cold_start_time),
            "short_circuit_time": float(self.short_circuit_time),
            "config_path": self._config_path(),
            "callback_dir": self._callback_dir(),
            "log_dir": self._log_dir(),
            "get_sidecar_env": self.get_sidecar_env,
            "get_agent_env": self.get_agent_env,
            "get_sidecar_cmd_args": self.get_sidecar_cmd_args,
            "on_started": self.on_started,
            "on_stopping": self.on_stopping,
            "required_secrets": self.required_secrets(),
        }

    @classmethod
    def required_secrets(cls) -> list[tuple[str, str]]:
        """Return ``(key_name, description)`` for the secrets this provider needs."""
        if cls.secret_description:
            return [("api_key", cls.secret_description)]
        return []

    # --- Abstract hooks (subclasses must implement) ---

    @abstractmethod
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        """Return env vars for the sidecar container (upstream auth tokens)."""

    @abstractmethod
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        """Return env vars injected into the agent container."""

    @abstractmethod
    def get_sidecar_cmd_args(self) -> list[str]:
        """Return extra command-line args for the sidecar container entrypoint."""

    # --- Optional lifecycle hooks (overridden by subclasses) ---

    def on_started(self, master_key: str) -> None:
        """
        Run once, under the lock, right after the sidecar is started.

        Default no-op. Subclasses that must register the master key (e.g. approve
        it in .claude.json) override this — it runs exactly once per sidecar
        lifetime, not per agent.
        """

    def on_stopping(self, master_key: str) -> None:
        """
        Run once, under the lock, right before the sidecar is stopped.

        Default no-op. The inverse of on_started (e.g. un-approve the master key).
        """

    # --- Config resolution (introspect the provider subclass module) ---

    def _config_path(self) -> Path:
        """Resolve config.yaml next to this provider's provider.py."""
        provider_dir = (
            TOOL_DIR
            / "agent_wrap"
            / "domain"
            / "providers"
            / self.__class__.__module__.split(".")[-2]
        )
        return provider_dir / "config.yaml"

    def _state_dir(self) -> Path:
        """Resolve the provider's source directory (for lock/activity/state files)."""
        return (
            TOOL_DIR
            / "agent_wrap"
            / "domain"
            / "providers"
            / self.__class__.__module__.split(".")[-2]
        )

    def _callback_dir(self) -> Path:
        """Resolve the shared LiteLLM logging callback (mounted into the sidecar)."""
        return Path(__file__).parent / "litellm_common" / "litellm_runtime"

    def _tool_dir(self) -> Path:
        """Resolve the agent-wrap install/repo root (e.g. /workspace)."""
        return TOOL_DIR

    def _log_dir(self) -> Path:
        """
        Shared host directory bind-mounted into the sidecar at /var/log/agent-wrap.

        Project-independent: a single directory under the agent-wrap install root.
        The callback writes to <project_hash>/<provider>/<session_id>/ beneath it,
        using the x-agent-wrap-log-prefix header the wrapper injects per launch and
        the AGENT_WRAP_PROVIDER env var set on the sidecar. This is required because
        a single shared sidecar (first-launch-wins) serves every project on the host.
        """
        return self._tool_dir() / "litellm-logs"
