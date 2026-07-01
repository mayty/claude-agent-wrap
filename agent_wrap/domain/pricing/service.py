# This file has been edited with the assistance of an AI tool.
"""Shared pricing and token-extraction utilities — domain service."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from agent_wrap.domain.pricing.constants import (
    DATE_SUFFIX_RE,
    MODEL_CONTEXT_SUFFIX_RE,
    MODEL_FAMILY_RE_T_FIRST,
    MODEL_FAMILY_RE_V_FIRST,
)
from agent_wrap.domain.pricing.models import Bucket

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agent_wrap.domain.providers.service import ProviderService


class PricingService:
    """
    Domain service wrapping pricing lookup, usage extraction, and cost computation.

    This is the *single public API* for the pricing subpackage.  Every consumer
    outside ``agent_wrap.domain.pricing`` accesses pricing functionality through
    an injected ``PricingService`` instance — never by importing the internal
    module-level helpers directly.
    """

    # Bucket factory for cross-domain consumers (accessed via injected instance).
    def new_bucket(self) -> Bucket:
        """Return a fresh, empty :class:`Bucket` for token-count accumulation."""
        return Bucket()

    def __init__(self, provider_service: ProviderService) -> None:
        self._provider_service = provider_service
        # provider_name -> {model_key: [tier, ...]}, or None if unavailable.
        # Key presence (even with a None value) marks the provider as fetched.
        self._cache: dict[str, dict[str, list[dict[str, Any]]] | None] = {}
        # Per-instance warning state (print once).
        self._mixed_cache_ttl_warned = False
        self._usage_convention_warned = False

    # ------------------------------------------------------------------
    # Cache TTL helpers (inlined from UsageCollectors)
    # ------------------------------------------------------------------

    def _collect_cache_ttls(self, node: Any, out: set[str]) -> None:
        """Recursively gather every ``cache_control`` breakpoint's TTL into *out*."""
        if isinstance(node, dict):
            cc = node.get("cache_control")
            if isinstance(cc, dict):
                out.add("1h" if cc.get("ttl") == "1h" else "5m")
            for key, value in node.items():
                if key == "cache_control":
                    continue
                self._collect_cache_ttls(value, out)
        elif isinstance(node, list):
            for item in node:
                self._collect_cache_ttls(item, out)

    def request_cache_ttl(self, request: dict[str, Any] | None) -> str | None:
        """
        Determine the cache-write TTL tier a request asked for from its markers.

        Returns ``"5m"`` or ``"1h"`` when all breakpoints agree, ``"mixed"`` when
        they disagree, or ``None`` when the request carries no ``cache_control``
        markers at all.
        """
        if not isinstance(request, dict):
            return None
        body = request.get("body")
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return None
        ttls: set[str] = set()
        self._collect_cache_ttls(data, ttls)
        if not ttls:
            return None
        if len(ttls) > 1:
            return "mixed"
        return ttls.pop()

    def response_cache_split(self, usage: dict[str, Any]) -> dict[str, int]:
        """
        Read the response's ephemeral 5m/1h cache-write split, if it reports one.

        Returns an empty dict when no split is present.
        """
        split: dict[str, int] = {}
        for source in (usage.get("cache_creation"), usage):
            if not isinstance(source, dict):
                continue
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"):
                if key in source:
                    split[key] = source[key]
        return split

    # ------------------------------------------------------------------
    # Model normalization
    # ------------------------------------------------------------------

    def normalize_model(self, model: str) -> str | None:
        """
        Return a canonical 'claude-<tier>-<ver>' key for a session model id.

        Handles the various forms session JSONLs surface:
          claude-opus-4-7
          claude-sonnet-4-5-20250929          (date-stamped snapshot)
          anthropic.claude-opus-4-7-v1:0      (Bedrock model id form)
          Claude Opus 4.7                     (display name)

        Returns None if `model` doesn't look like a Claude release.
        """
        if not model:
            return None
        bare = DATE_SUFFIX_RE.sub("", model)
        m = MODEL_FAMILY_RE_T_FIRST.search(bare) or MODEL_FAMILY_RE_V_FIRST.search(bare)
        if not m:
            return None
        tier = m.group("tier").lower()
        ver = m.group("ver").replace(".", "-")
        return f"claude-{tier}-{ver}"

    def _best_prefix_key(self, query: str, keys: Iterable[str]) -> str | None:
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

    # ------------------------------------------------------------------
    # Pricing lookup (inlined from PriceSource)
    # ------------------------------------------------------------------

    def get_pricing(self, provider: str, model: str) -> list[dict[str, Any]] | None:
        """Return the tier list for *model*, or None if no price is known."""
        if provider not in self._cache:
            self._cache[provider] = self._fetch_pricing(provider)
        table = self._cache[provider]
        if not table:
            return None

        clean = model.rsplit("/", 1)[-1]
        candidates = [
            self.normalize_model(clean),
            MODEL_CONTEXT_SUFFIX_RE.sub("", clean),
            clean,
        ]
        for key in candidates:
            if not key:
                continue
            match = self._best_prefix_key(key, table)
            if match is not None:
                return table[match]
        return None

    def _fetch_pricing(self, provider: str) -> dict[str, list[dict[str, float]]]:
        """Build the unified tiered table for one provider (fetched once)."""
        try:
            p = self._provider_service.get_provider(provider)
            flat = p.get_pricing()
            tiered = p.get_tiered_pricing()
        except Exception:  # noqa: BLE001
            return {}

        table: dict[str, list[dict[str, float]]] = {}
        # Flat rates first, recast as a single infinite tier…
        for model_key, rates in (flat or {}).items():
            table[model_key] = [
                {
                    "max_in": float("inf"),
                    "in": rates["in"],
                    "out": rates["out"],
                    "cw_5m": rates["cw_5m"],
                    "cw_1h": rates["cw_1h"],
                    "cr": rates["cr"],
                }
            ]
        # …then let genuine tiered pricing override flat for the same model.
        for model_key, entry in (tiered or {}).items():
            if entry and "tiers" in entry:
                table[model_key] = entry["tiers"]
        return table

    # ------------------------------------------------------------------
    # Cost computation
    # ------------------------------------------------------------------

    def compute_cost(
        self,
        provider: str,
        model: str,
        raw_response: dict[str, Any] | None = None,
        request_ttl: str | None = None,
        *,
        usage: dict[str, Any] | None = None,
    ) -> float | None:
        """
        Compute the USD cost of a single request, or None if pricing is unknown.

        When *usage* is given it is used directly (skipping ``extract_usage``),
        letting callers that already extracted usage avoid redundant work.
        """
        tiers = self.get_pricing(provider, model.rsplit("/", 1)[-1])
        if tiers is None:
            return None
        if usage is None:
            usage = self.extract_usage(raw_response, request_ttl)
        return self.cost_for_tiers(tiers, usage)

    # ------------------------------------------------------------------
    # Usage extraction
    # ------------------------------------------------------------------

    def extract_usage(
        self, response: dict[str, Any] | None, request_ttl: str | None = None
    ) -> dict[str, Any]:
        """Extract and normalize usage dict from a LiteLLM response object."""
        if not response or not isinstance(response, dict):
            return {}
        usage = response.get("usage")
        if not usage or not isinstance(usage, dict):
            return {}

        cache_creation = self.response_cache_split(usage)

        in_tokens = usage.get("input_tokens") or usage.get("prompt_tokens") or 0
        out_tokens = usage.get("output_tokens") or usage.get("completion_tokens") or 0
        cw_tokens = usage.get("cache_creation_input_tokens") or 0
        cr_tokens = usage.get("cache_read_input_tokens") or 0

        # When the response gave no ephemeral breakdown (the Bedrock/LiteLLM case),
        # attribute the flat cache-write total to the tier the request asked for.
        if not cache_creation and cw_tokens and request_ttl:
            if request_ttl == "mixed":
                self._warn_mixed_cache_ttl()
            elif request_ttl == "1h":
                cache_creation["ephemeral_1h_input_tokens"] = cw_tokens
            else:
                cache_creation["ephemeral_5m_input_tokens"] = cw_tokens

        return {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "cache_creation_input_tokens": cw_tokens,
            "cache_read_input_tokens": cr_tokens,
            "cache_creation": cache_creation,
        }

    def cost_for_tiers(self, tiers: list[dict[str, Any]], usage: dict[str, Any]) -> float:
        """Calculate the cost of a single request given its applicable tier list."""
        in_tokens = usage.get("input_tokens", 0) or 0
        out_tokens = usage.get("output_tokens", 0) or 0
        cr_tokens = usage.get("cache_read_input_tokens", 0) or 0

        cc = usage.get("cache_creation") or {}
        cw_5m = cc.get("ephemeral_5m_input_tokens", 0) or 0
        cw_1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
        if not (cw_5m or cw_1h):
            cw_5m = usage.get("cache_creation_input_tokens", 0) or 0

        if not (in_tokens or out_tokens or cw_5m or cw_1h or cr_tokens):
            return 0.0

        tier = next((t for t in tiers if in_tokens <= t["max_in"]), tiers[-1])

        fresh_in_tokens = in_tokens - cw_5m - cw_1h - cr_tokens
        if fresh_in_tokens < 0:
            self._warn_usage_convention_drift(in_tokens, cw_5m, cw_1h, cr_tokens)
            fresh_in_tokens = 0

        return (
            fresh_in_tokens * tier["in"] / 1_000_000
            + out_tokens * tier["out"] / 1_000_000
            + cw_5m * tier["cw_5m"] / 1_000_000
            + cw_1h * tier["cw_1h"] / 1_000_000
            + cr_tokens * tier["cr"] / 1_000_000
        )

    def _warn_mixed_cache_ttl(self) -> None:
        """Print a warning (once) when a request mixed 5m and 1h cache TTLs."""
        if self._mixed_cache_ttl_warned:
            return
        self._mixed_cache_ttl_warned = True
        print(
            "warning: a request mixed 5m and 1h cache TTLs, but the response reports "
            "only a flat cache-write total. Those writes are priced at the 5m rate; "
            "reported cache-write cost may be slightly low. See "
            "agent_wrap/lib/usage.py:request_cache_ttl.",
            file=sys.stderr,
        )

    def _warn_usage_convention_drift(
        self, in_tokens: int, cw_5m: int, cw_1h: int, cr_tokens: int
    ) -> None:
        """Print a warning (once) when the token-overlap assumption appears broken."""
        if self._usage_convention_warned:
            return
        self._usage_convention_warned = True
        print(
            "warning: token usage convention drift detected — "
            f"input_tokens ({in_tokens}) < cache-write ({cw_5m + cw_1h}) + "
            f"cache-read ({cr_tokens}). Cost math assumes input_tokens is inclusive of "
            "cache tokens; this record violates that. Reported costs may be inaccurate "
            "until agent_wrap/domain/pricing/pricing.py:cost_for_tiers is revisited.",
            file=sys.stderr,
        )
