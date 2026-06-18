# This file has been edited with the assistance of an AI tool.
"""Provider interface definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_wrap.sidecars.base import Sidecar


class Provider(ABC):
    """
    Abstract base class for model-routing providers.

    Each provider declares the sidecars an agent run depends on (e.g. a LiteLLM
    proxy fronting the model API). The launcher collects those sidecars, ensures
    each before docker run, splices the connectivity flags each returns into the
    agent's docker run command, and releases each after the agent exits.
    """

    #: Provider name matching the AGENT_PROVIDER env var (e.g. "litellm-bedrock").
    name: str

    @abstractmethod
    def sidecars(self) -> list[Sidecar]:
        """Return the sidecars an agent run with this provider depends on."""

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
