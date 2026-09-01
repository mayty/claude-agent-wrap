# This file has been edited with the assistance of an AI tool.
"""Shared pricing and token-extraction utilities — domain service."""

from typing import TYPE_CHECKING, Any

from agent_wrap.domain.pricing.constants import (
    DATE_SUFFIX_RE,
    MODEL_FAMILY_RE_T_FIRST,
    MODEL_FAMILY_RE_V_FIRST,
)
from agent_wrap.domain.pricing.models import Bucket, TokenUsage

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.providers.service import ProviderService


class PricingService:
    """
    Domain service wrapping usage extraction, model normalization, and cost lookup.

    This is the *single public API* for the pricing subpackage.  Every consumer
    outside ``agent_wrap.domain.pricing`` accesses pricing functionality through
    an injected ``PricingService`` instance — never by importing the internal
    module-level helpers directly.

    Cost computation is delegated to the provider (``Provider.compute_cost``)
    so that individual providers can apply custom pricing logic (e.g. time-of-day
    multipliers).
    """

    # Bucket factory for cross-domain consumers (accessed via injected instance).
    def new_bucket(self) -> Bucket:
        """Return a fresh, empty :class:`Bucket` for token-count accumulation."""
        return Bucket()

    def bucket_from_usage(self, usage: TokenUsage, *, msgs: int, unrecorded: int = 0) -> Bucket:
        """
        Return a Bucket holding an already-aggregated *msgs* requests' worth of *usage*.

        For callers that hold pre-summed token totals rather than per-request
        usage (e.g. the stats usage archive). Token math still goes through
        ``Bucket.add`` so its 5m/1h cache-write tier attribution stays the single
        source of truth; only the two counters that cannot be derived from token
        counts are then set explicitly, since ``add`` counts exactly one message.
        """
        bucket = Bucket()
        bucket.add(usage, 0.0)
        bucket.msgs = msgs
        bucket.unrecorded = unrecorded
        return bucket

    def __init__(self, provider_service: ProviderService, display_service: DisplayService) -> None:
        self._provider_service = provider_service
        self._display = display_service
        # Per-instance warning state (print once).
        self._mixed_cache_ttl_warned = False

    # ------------------------------------------------------------------
    # Cache TTL helpers (inlined from UsageCollectors)
    # ------------------------------------------------------------------

    def _collect_cache_ttls(self, node: Any, out: set[str]) -> None:
        """
        Recursively gather every ``cache_control`` breakpoint's TTL into *out*.

        *node* is a recursively nested JSON-like structure (dict/list/scalar) —
        ``Any`` is the honest type for arbitrary-depth traversal.
        """
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

    # ------------------------------------------------------------------
    # Cost computation (delegates to provider)
    # ------------------------------------------------------------------

    def compute_cost(  # noqa: PLR0913
        self,
        provider: str,
        model: str,
        *,
        usage: TokenUsage,
        hour: int | None,
        weekday: int | None = None,
        refresh_pricing_data: bool = False,
    ) -> float | None:
        """
        Compute the USD cost of a single request, or None if pricing is unknown.

        *hour* is the UTC hour the usage belongs to — the half-open interval
        ``[hour, hour+1)`` — and may be None when the record's timestamp is
        unknown. *weekday* is the UTC weekday (``datetime.weekday()``: 0=Monday
        ... 6=Sunday), or None when unknown. Providers that price by time-of-day
        use them; flat-rate providers ignore them.

        Normalizes *model* (Claude display names → canonical keys) then delegates
        to the provider's ``compute_cost`` method.  Callers must extract usage
        first via :meth:`extract_usage`.

        Provider instances come from ``ProviderService.get_provider``, which
        caches them, so the provider-level ``@cache`` on pricing-table
        construction is shared across calls. When ``refresh_pricing_data`` is
        set, it is passed down so the provider re-fetches its pricing once (the
        first call repopulates the cache; subsequent calls hit it).
        """
        clean = model.rsplit("/", 1)[-1]
        normalized = self.normalize_model(clean) or clean
        try:
            p = self._provider_service.get_provider(provider)
        except Exception:  # noqa: BLE001
            # Provider lookup is best-effort — any failure (unknown provider,
            # misconfiguration, etc.) should silently fall back to unknown cost.
            return None
        return p.compute_cost(
            normalized,
            usage,
            hour=hour,
            weekday=weekday,
            refresh_pricing_data=refresh_pricing_data,
        )

    # ------------------------------------------------------------------
    # Usage extraction
    # ------------------------------------------------------------------

    def extract_usage(
        self, response: dict[str, Any] | None, request_ttl: str | None = None
    ) -> TokenUsage:
        """Extract and normalize usage dict from a LiteLLM response object."""
        _zero: TokenUsage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        }
        if not response or not isinstance(response, dict):
            return _zero
        usage = response.get("usage")
        if not usage or not isinstance(usage, dict):
            return _zero

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

    def _warn_mixed_cache_ttl(self) -> None:
        """Emit a warning (once) when a request mixed 5m and 1h cache TTLs."""
        if self._mixed_cache_ttl_warned:
            return
        self._mixed_cache_ttl_warned = True
        self._display.warning(
            "a request mixed 5m and 1h cache TTLs, but the response reports "
            "only a flat cache-write total. Those writes are priced at the 5m rate; "
            "reported cache-write cost may be slightly low. See "
            "agent_wrap/lib/usage.py:request_cache_ttl."
        )
