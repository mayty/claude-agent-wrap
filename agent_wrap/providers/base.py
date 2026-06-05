# This file has been created with the assistance of an AI tool.
"""Provider interface definition."""

from __future__ import annotations

from abc import ABC, abstractmethod


class Provider(ABC):
    """
    Abstract base class for model-routing providers.

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
        *,
        use_host_net: bool,
        instance_id: str,
        agent_network: str | None,
    ) -> None:
        """Ensure the backend is ready. Raises on failure."""

    @abstractmethod
    def release(self, instance_id: str) -> None:
        """Clean up after the agent exits."""

    @abstractmethod
    def get_run_args(self) -> list[str]:
        """Return extra docker run flags (env vars, network flags, etc.)."""

    @abstractmethod
    def get_label_args(self, instance_id: str) -> list[str]:
        """Return --label and --name flags for the agent container."""

    def get_pricing(self) -> dict[str, dict[str, float]]:
        """
        Return a pricing table for this provider.

        Keys are canonical model identifiers (e.g., 'claude-sonnet-4-5').
        Values are dicts with keys: 'in', 'out', 'cw_5m', 'cw_1h', 'cr'
        representing the cost per 1 million tokens.

        Returns an empty dict if pricing is not available or not implemented.
        """
        return {}

    def get_tiered_pricing(self) -> dict | None:
        """
        Return a tiered pricing table for this provider, if applicable.

        Keys are canonical model identifiers.
        Values are dicts with a 'tiers' key containing a list of tier dicts.
        Each tier dict has 'max_in' (token threshold) and 'in', 'out', 'cw_5m',
        'cw_1h', 'cr' representing the cost per 1 million tokens for that tier.

        Returns None if tiered pricing is not available or not implemented.
        """
        return None
