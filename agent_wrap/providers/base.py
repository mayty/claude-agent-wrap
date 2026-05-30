# This file has been created with the assistance of an AI tool.
"""Provider interface definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class Provider(ABC):
    """Abstract base class for model-routing providers.

    Each provider manages a backend (e.g., a LiteLLM sidecar) that fronts
    the actual model API. The launcher calls ensure() before docker run,
    release() after it exits, and splices get_run_args() / get_label_args()
    into the agent's docker run command.
    """

    #: Provider name matching the AGENT_PROVIDER env var (e.g. "litellm-bedrock").
    name: str

    @abstractmethod
    def ensure(
        self,
        tool_dir: Path,
        use_host_net: bool,
        instance_id: str,
        agent_network: str | None,
    ) -> None:
        """Ensure the backend is ready. Raises on failure."""

    @abstractmethod
    def release(self, tool_dir: Path, instance_id: str) -> None:
        """Clean up after the agent exits."""

    @abstractmethod
    def get_run_args(self) -> list[str]:
        """Return extra docker run flags (env vars, network flags, etc.)."""

    @abstractmethod
    def get_label_args(self, instance_id: str) -> list[str]:
        """Return --label and --name flags for the agent container."""
