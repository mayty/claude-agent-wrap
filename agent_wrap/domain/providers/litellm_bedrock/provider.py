# This file has been edited with the assistance of an AI tool.
"""LiteLLM Bedrock provider — routes Claude Code through AWS Bedrock."""

from __future__ import annotations

import gzip
import html
import json
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from pathlib import Path

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.litellm_bedrock.constants import (
    DEFAULT_REGION_LABEL,
    MODEL_KEY_RE,
    MODEL_NAME_RE,
    PRICING_CACHE_TTL_SECONDS,
    PRICING_DATA_URL,
    PRICING_FETCH_TIMEOUT,
    PRICING_PAGE_URL,
    PRICING_SCHEMAS,
    ROW_RE,
    SECTION_RE,
)


class _BedrockPricing:
    """Scrapes AWS Bedrock pricing and caches the result."""

    @staticmethod
    def http_get(url: str) -> bytes:
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "User-Agent": "agent-wrap/agent_usage",
                "Accept-Encoding": "gzip",
            },
        )
        with urllib.request.urlopen(req, timeout=PRICING_FETCH_TIMEOUT) as resp:  # noqa: S310
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    @staticmethod
    def scrape_model_keys(page_html: str) -> dict[str, tuple[tuple[str, ...], list[str]]]:
        src = html.unescape(page_html).replace("\\u003c", "<").replace("\\u003e", ">")
        src = src.replace('\\"', '"')

        section_starts: list[tuple[int, str]] = [
            (m.start(), "geo" if m.group(1).lower().startswith("geo") else "global")
            for m in SECTION_RE.finditer(src)
        ]

        def section_at(pos: int) -> str:
            cur = "global"
            for start, name in section_starts:
                if start <= pos:
                    cur = name
                else:
                    break
            return cur

        rank = {"geo": 1, "global": 0}
        out: dict[str, tuple[int, tuple[str, ...], list[str]]] = {}
        for m in ROW_RE.finditer(src):
            row = m.group("row")
            nm = MODEL_NAME_RE.search(row)
            if not nm:
                continue
            keys = MODEL_KEY_RE.findall(row)
            schema = PRICING_SCHEMAS.get(len(keys))
            if schema is None:
                continue
            tier_name = section_at(m.start())
            tier_rank = rank[tier_name]
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
            except (KeyError, TypeError, ValueError):
                continue
            table[canonical] = row
        return table

    @staticmethod
    def load_prices(
        cache_path: Path, *, refresh_pricing_data: bool = False
    ) -> dict[str, dict[str, float]]:
        cached: dict[str, Any] | None = None
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = None

        fresh_enough = (
            cached is not None
            and cached.get("region") == DEFAULT_REGION_LABEL
            and isinstance(cached.get("fetched_at"), (int, float))
            and (time.time() - cached["fetched_at"]) < PRICING_CACHE_TTL_SECONDS
        )

        if not refresh_pricing_data and fresh_enough and cached is not None:
            return cached.get("prices") or {}

        try:
            page = _BedrockPricing.http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")
            data = json.loads(_BedrockPricing.http_get(PRICING_DATA_URL))
            prices = _BedrockPricing.build_pricing_table(page, data, DEFAULT_REGION_LABEL)
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            if cached:
                return cached.get("prices") or {}
            return {}

        if not prices:
            if cached:
                return cached.get("prices") or {}
            return {}

        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"region": DEFAULT_REGION_LABEL, "fetched_at": time.time(), "prices": prices},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        return prices


class BedrockProvider(Provider):
    name = "litellm-bedrock"
    secret_description: ClassVar[str] = "AWS Bedrock Bearer Token"  # noqa: S105

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": secrets.get("api_key", ""),
            "AWS_REGION_NAME": "us-east-1",
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": master_key,
            "ANTHROPIC_BEDROCK_BASE_URL": f"{base_url}/bedrock",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-east-1",
        }

    def _get_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, dict[str, float]]:
        """Return the cached AWS Bedrock pricing table for this provider."""
        cache_path = self._state_dir() / "pricing.json"
        return _BedrockPricing.load_prices(cache_path, refresh_pricing_data=refresh_pricing_data)
