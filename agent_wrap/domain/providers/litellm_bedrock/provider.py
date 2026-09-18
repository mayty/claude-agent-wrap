# This file has been edited with the assistance of an AI tool.
"""LiteLLM Bedrock provider — routes Claude Code through AWS Bedrock."""

import html
import json
from typing import TYPE_CHECKING, Any, ClassVar, override

from bs4 import BeautifulSoup

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.constants import HTML_PARSER
from agent_wrap.domain.providers.litellm_bedrock.constants import (
    DEFAULT_REGION_LABEL,
    GEO_SECTION,
    GLOBAL_SECTION,
    JSON_ESCAPED_MARKUP,
    MODEL_KEY_RE,
    MODEL_NAME_RE,
    PRICING_DATA_URL,
    PRICING_PAGE_URL,
    PRICING_SCHEMAS,
    SECTION_HEADING_RE,
    SECTION_RANK,
)
from agent_wrap.domain.providers.pricing import PricingCache

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.providers.models import PriceTable


class _BedrockPricing:
    """Scrapes AWS Bedrock pricing and caches the result."""

    @staticmethod
    def scrape_model_keys(page_html: str) -> dict[str, tuple[tuple[str, ...], list[str]]]:
        """
        Map each Claude model on the pricing page to its column schema and price keys.

        Headings and rows are walked in document order, so the ``<h2>`` a row sits under
        is simply whichever one came before it -- the tier a price belongs to, which no
        amount of looking at the row alone can tell.
        """
        src = html.unescape(page_html)
        for escaped, literal in JSON_ESCAPED_MARKUP:
            src = src.replace(escaped, literal)

        section = GLOBAL_SECTION
        out: dict[str, tuple[int, tuple[str, ...], list[str]]] = {}
        for node in BeautifulSoup(src, HTML_PARSER).find_all(["h2", "tr"]):
            text = node.get_text(" ", strip=True)
            if node.name == "h2":
                if SECTION_HEADING_RE.match(text):
                    section = GEO_SECTION if text.lower().startswith("geo") else GLOBAL_SECTION
                continue
            nm = MODEL_NAME_RE.search(text)
            if not nm:
                continue
            keys = MODEL_KEY_RE.findall(text)
            schema = PRICING_SCHEMAS.get(len(keys))
            if schema is None:
                continue
            tier_rank = SECTION_RANK[section]
            canonical = f"claude-{nm.group(1).lower()}-{nm.group(2).replace('.', '-')}"
            prev = out.get(canonical)
            if prev is None or tier_rank > prev[0]:
                out[canonical] = (tier_rank, schema, keys)
        return {k: (s, ks) for k, (_, s, ks) in out.items()}

    @staticmethod
    def build_pricing_table(
        page_html: str, data_json: dict[str, Any], region_label: str
    ) -> dict[str, dict[str, float]]:
        region = data_json.get("regions", {}).get(region_label) or {}
        keys_by_model = _BedrockPricing.scrape_model_keys(page_html)

        table: dict[str, dict[str, float]] = {}
        for canonical, (schema, keys) in keys_by_model.items():
            cols = dict(zip(schema, keys, strict=True))
            try:
                row = {
                    "in": float(region[cols["in"]]["price"]),
                    "out": float(region[cols["out"]]["price"]),
                    "cw_5m": float(region[cols["cw_5m"]]["price"]),
                    "cw_1h": float(region[cols["cw_1h"]]["price"]),
                    "cr": float(region[cols["cr"]]["price"]),
                }
            except KeyError, TypeError, ValueError:
                continue
            table[canonical] = row
        return table

    @staticmethod
    def scrape() -> tuple[PriceTable, dict[str, Any]]:
        """
        Join the pricing page's placeholders against the metered-unit map. Two fetches.

        The region travels into the cached document so a table scraped for one region is
        not served for another -- see ``load_prices``.
        """
        page = PricingCache.http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")
        data = json.loads(PricingCache.http_get(PRICING_DATA_URL))
        prices = _BedrockPricing.build_pricing_table(page, data, DEFAULT_REGION_LABEL)
        return prices, {"region": DEFAULT_REGION_LABEL}

    @staticmethod
    def load_prices(cache_path: Path, *, refresh_pricing_data: bool = False) -> PriceTable:
        return PricingCache.load(
            cache_path,
            refresh=refresh_pricing_data,
            scrape=_BedrockPricing.scrape,
            # A document from a different region is fresh and useless at once: every
            # figure in it is a price somewhere the agent is not running.
            still_valid=lambda cached: cached.get("region") == DEFAULT_REGION_LABEL,
        )


class BedrockProvider(Provider):
    name = "litellm-bedrock"
    secret_description: ClassVar[str] = "AWS Bedrock Bearer Token"  # noqa: S105

    @override
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": secrets.get("api_key", ""),
            "AWS_REGION_NAME": "us-east-1",
        }

    @override
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": master_key,
            "ANTHROPIC_BEDROCK_BASE_URL": f"{base_url}/bedrock",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-east-1",
        }

    @override
    def _get_pricing(self, *, refresh_pricing_data: bool = False) -> PriceTable:
        return _BedrockPricing.load_prices(
            self._pricing_cache_path(), refresh_pricing_data=refresh_pricing_data
        )
