# This file has been created with the assistance of an AI tool.
"""
Pricing arithmetic and pricing-table caching for providers.

Three stateless namespace classes used by ``Provider``: ``ModelKeyMatcher`` resolves a
request's model identifier to a pricing-table key, ``CostComputer`` turns a tier plus a
token-usage record into a USD cost, and ``PricingCache`` fetches and caches a scraped
table for the providers whose prices are not a literal. None knows anything about
sidecars or provider identity.
"""

import json
import time
from typing import TYPE_CHECKING, Any

import httpx2

from agent_wrap.domain.providers.constants import (
    PRICING_CACHE_TTL_SECONDS,
    PRICING_FETCH_TIMEOUT,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from pathlib import Path

    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.providers.models import PriceTable, Tier


class ModelKeyMatcher:
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


class PricingCache:
    """
    Fetching and caching a scraped pricing table, for the providers that scrape one.

    The protocol is the same for every such provider: read the cached document, use it
    while it is fresh, otherwise scrape and write a new one -- and on any failure fall
    back to whatever stale document is on disk rather than to no prices at all, because a
    week-old table is a far better answer than reporting every request as unpriced.
    """

    @staticmethod
    def http_get(url: str) -> bytes:
        """
        Fetch *url* and return its raw bytes.

        ``raise_for_status`` is not optional here: unlike urlopen, httpx2 returns a 4xx
        or 5xx as an ordinary response, so without it an error page would be handed to
        the caller's scraper and parsed as pricing.
        """
        response = httpx2.get(
            url,
            headers={"User-Agent": "agent-wrap/agent_usage"},
            timeout=PRICING_FETCH_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.content

    @staticmethod
    def load(
        cache_path: Path,
        *,
        refresh: bool,
        scrape: Callable[[], tuple[PriceTable, dict[str, Any]]],
        still_valid: Callable[[dict[str, Any]], bool] = lambda _cached: True,
    ) -> PriceTable:
        """
        Return the cached prices at *cache_path*, scraping when they are stale or absent.

        *scrape* returns the table plus any extra fields that provider persists alongside
        it -- a region label, off-peak hours -- which are written into the same document
        and handed back to *still_valid* on the next read. *still_valid* is the provider's
        own freshness test on top of the TTL: a document scraped for a different region
        is fresh and useless at once.

        An empty scrape counts as a failure, not as "this provider costs nothing".
        """
        cached: dict[str, Any] | None = None
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                cached = None

        def stale() -> PriceTable:
            return (cached or {}).get("prices") or {}

        fresh_enough = (
            cached is not None
            and isinstance(cached.get("fetched_at"), (int, float))
            and (time.time() - cached["fetched_at"]) < PRICING_CACHE_TTL_SECONDS
            and still_valid(cached)
        )
        if not refresh and fresh_enough:
            return stale()

        try:
            prices, extra = scrape()
        except httpx2.HTTPError, OSError, json.JSONDecodeError:
            return stale()
        if not prices:
            return stale()

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"fetched_at": time.time(), "prices": prices, **extra},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass  # an unwritable cache costs a re-scrape next time, nothing more

        return prices
