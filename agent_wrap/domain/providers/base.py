# This file has been edited with the assistance of an AI tool.
"""Provider interface definition."""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import cache
from typing import TYPE_CHECKING

from agent_wrap.domain.providers.constants import MODEL_CONTEXT_SUFFIX_RE

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.providers.models import Tier
    from agent_wrap.domain.sidecars.base import Sidecar
    from agent_wrap.domain.sidecars.service import SidecarService


class _ModelKeyMatcher:
    """Model-key prefix matching for pricing table lookups."""

    @staticmethod
    def best_prefix_key(query: str, keys: Iterable[str]) -> str | None:
        """
        Pick the pricing-table key that best matches *query* under true-prefix matching.

        A key matches when it is a prefix of the query or the query is a prefix of it,
        so a date-stamped request id matches its base pricing key, and a base request
        matches its newest date-stamped key. Among matches prefer, in order:
          1. the longest shared prefix,
          2. then the shortest key (an exact base key beats a longer date-stamped one),
          3. then the alphabetically-greatest key (newer date suffix wins).
        """
        best: str | None = None
        best_rank: tuple[int, int, str] | None = None
        for k in keys:
            if not (k.startswith(query) or query.startswith(k)):
                continue
            rank = (min(len(k), len(query)), -len(k), k)
            if best_rank is None or rank > best_rank:
                best, best_rank = k, rank
        return best


class _CostComputer:
    """Token cost computation from tiered pricing data."""

    @staticmethod
    def cost_for_tiers(
        tiers: list[Tier],
        usage: TokenUsage,
    ) -> tuple[float, bool]:
        """
        Calculate the cost of a single request given its applicable tier list.

        *tiers* must be sorted by ``max_in`` (ascending). The first tier whose
        ``max_in >= input_tokens`` wins; the last tier is the fallback.

        Returns ``(cost, convention_warning_needed)``. The caller is responsible
        for issuing the convention-drift warning at most once per provider instance.
        """
        in_tokens: int = usage["input_tokens"]
        out_tokens: int = usage["output_tokens"]
        cr_tokens: int = usage["cache_read_input_tokens"]

        cc = usage.get("cache_creation", {})
        cw_5m: int = cc.get("ephemeral_5m_input_tokens", 0) or 0
        cw_1h: int = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if not (cw_5m or cw_1h):
            cw_5m = usage.get("cache_creation_input_tokens", 0)

        if not (in_tokens or out_tokens or cw_5m or cw_1h or cr_tokens):
            return 0.0, False

        tier = next((t for t in tiers if in_tokens <= t["max_in"]), tiers[-1])

        fresh_in_tokens = in_tokens - cw_5m - cw_1h - cr_tokens
        convention_warn = fresh_in_tokens < 0

        fresh_in_tokens = max(fresh_in_tokens, 0)

        cost = (
            fresh_in_tokens * tier["in_"] / 1_000_000
            + out_tokens * tier["out"] / 1_000_000
            + cw_5m * tier["cw_5m"] / 1_000_000
            + cw_1h * tier["cw_1h"] / 1_000_000
            + cr_tokens * tier["cr"] / 1_000_000
        )
        return cost, convention_warn


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

    def __init__(
        self,
        sidecar_service: SidecarService,
        display_service: DisplayService,
    ) -> None:
        self._sidecar_service = sidecar_service
        self._display = display_service
        self._usage_convention_warned = False

    @classmethod
    def required_secrets(cls) -> list[tuple[str, str]]:
        """Return ``(key_name, description)`` tuples for secrets this provider needs."""
        return []

    @abstractmethod
    def sidecars(self) -> list[Sidecar]:
        """Return the sidecars an agent run with this provider depends on."""

    # ------------------------------------------------------------------
    # Raw pricing data (subclass contract)
    # ------------------------------------------------------------------

    def _get_pricing(self) -> dict[str, dict[str, float]]:
        """
        Return a flat pricing table for this provider.

        Keys are canonical model identifiers (e.g., 'claude-sonnet-4-5').
        Values are dicts with keys: 'in', 'out', 'cw_5m', 'cw_1h', 'cr'
        representing the cost per 1 million tokens.

        Raises ``NotImplementedError`` by default — providers that support
        flat-rate pricing must override.
        """
        raise NotImplementedError

    def _get_tiered_pricing(self) -> dict[str, list[Tier]]:
        """
        Return a tiered pricing table for this provider.

        Keys are canonical model identifiers.  Values are lists of
        :class:`Tier` dicts, each with 'max_in' (token threshold), 'in_',
        'out', 'cw_5m', 'cw_1h', and 'cr' fields.

        Raises ``NotImplementedError`` by default — providers that support
        tiered pricing must override.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Pricing table construction
    # ------------------------------------------------------------------

    @cache  # noqa: B019
    def _build_pricing_table(self) -> dict[str, list[Tier]]:
        """
        Build a unified tiered pricing table.

        Tries ``_get_tiered_pricing()`` first (already in the right shape).
        Falls back to ``_get_pricing()``, converting each flat-rate entry
        into a single infinite tier.  Returns an empty dict when neither
        method is implemented.
        """
        try:
            return self._get_tiered_pricing()
        except NotImplementedError:
            pass

        try:
            flat = self._get_pricing()
        except NotImplementedError:
            return {}

        table: dict[str, list[Tier]] = {}
        for model_key, rates in flat.items():
            table[model_key] = [
                {
                    "max_in": float("inf"),
                    "in_": rates["in"],
                    "out": rates["out"],
                    "cw_5m": rates["cw_5m"],
                    "cw_1h": rates["cw_1h"],
                    "cr": rates["cr"],
                }
            ]
        return table

    def _cost_for_tiers(self, tiers: list[Tier], usage: TokenUsage) -> float:
        """
        Calculate the cost of a single request given its applicable tier list.

        *tiers* must be sorted by ``max_in`` (ascending). The first tier whose
        ``max_in >= input_tokens`` wins; the last tier is the fallback.
        """
        cost, convention_warn = _CostComputer.cost_for_tiers(tiers, usage)
        if convention_warn and not self._usage_convention_warned:
            self._usage_convention_warned = True
            in_tokens: int = usage["input_tokens"]
            cc = usage.get("cache_creation", {})
            cw_5m: int = cc.get("ephemeral_5m_input_tokens", 0) or 0
            cw_1h: int = cc.get("ephemeral_1h_input_tokens", 0) or 0
            if not (cw_5m or cw_1h):
                cw_5m = usage.get("cache_creation_input_tokens", 0)
            cr_tokens: int = usage["cache_read_input_tokens"]
            self._display.warning(
                "token usage convention drift detected — "
                f"input_tokens ({in_tokens}) < cache-write ({cw_5m + cw_1h}) + "
                f"cache-read ({cr_tokens}). Cost math assumes input_tokens is "
                "inclusive of cache tokens; this record violates that. Reported "
                "costs may be inaccurate until "
                "agent_wrap/domain/providers/base.py:_CostComputer.cost_for_tiers is revisited."
            )
        return cost

    def compute_cost(self, model: str, usage: TokenUsage) -> float | None:
        """
        Compute the USD cost of a single request, or None if pricing is unknown.

        The default implementation builds the pricing table from
        ``_get_tiered_pricing()`` or ``_get_pricing()``, strips context-length
        suffixes from *model*, prefix-matches against pricing keys, selects the
        appropriate tier, and computes the cost.

        Subclasses can override this method to add custom logic (e.g., time-of-day
        multipliers).  *model* arrives already-normalized by ``PricingService``
        (Claude display names → canonical keys), but the default implementation
        still tolerates raw model names as a fallback.
        """
        table = self._build_pricing_table()
        if not table:
            return None

        # Try candidates in order: as-received, then [1m]-stripped.
        seen: set[str] = set()
        unique: list[str] = []
        for c in (model, MODEL_CONTEXT_SUFFIX_RE.sub("", model)):
            if c and c not in seen:
                seen.add(c)
                unique.append(c)

        tiers = None
        for key in unique:
            match = _ModelKeyMatcher.best_prefix_key(key, table)
            if match is not None:
                tiers = table[match]
                break

        if tiers is None:
            return None
        return self._cost_for_tiers(tiers, usage)
