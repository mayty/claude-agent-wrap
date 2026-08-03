# This file has been created with the assistance of an AI tool.
"""
Tiered-pricing arithmetic for providers.

Two stateless namespace classes used by ``Provider``: ``ModelKeyMatcher`` resolves a
request's model identifier to a pricing-table key, and ``CostComputer`` turns a tier
plus a token-usage record into a USD cost. Neither knows anything about sidecars or
provider identity — they are pure functions over a pricing table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.providers.models import Tier


class ModelKeyMatcher:
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


class CostComputer:
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

    @staticmethod
    def worst_case_cost(table: dict[str, list[Tier]], usage: TokenUsage) -> float:
        """
        Return the highest cost *usage* could incur under any tier this provider knows.

        Used when a model has no pricing-table match, to tell a genuinely
        negligible cost (rounds to $0 even at the priciest known rate) apart
        from a genuinely unknown one.
        """
        return max(
            (
                CostComputer.cost_for_tiers([tier], usage)[0]
                for tiers in table.values()
                for tier in tiers
            ),
            default=0.0,
        )
