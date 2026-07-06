# This file has been edited with the assistance of an AI tool.
"""LiteLLM DeepSeek provider — routes Claude Code through DeepSeek provider."""

from __future__ import annotations

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, ClassVar

from agent_wrap.domain.providers.key_approval import MasterKeyApprovalMixin
from agent_wrap.domain.providers.litellm_deepseek.constants import (
    MIN_MODEL_COUNT,
    PRICING_CACHE_TTL_SECONDS,
    PRICING_FETCH_TIMEOUT,
    PRICING_PAGE_URL,
)
from agent_wrap.domain.providers.litellm_provider import LiteLLMProvider

if TYPE_CHECKING:
    from pathlib import Path


class _DeepSeekPricing:
    """Scrapes the official DeepSeek pricing page and caches for 7 days."""

    @staticmethod
    def http_get(url: str) -> bytes:
        """Fetch *url* and return its raw bytes."""
        req = urllib.request.Request(  # noqa: S310
            url,
            headers={
                "User-Agent": "agent-wrap/agent_usage",
                "Accept-Encoding": "gzip",
            },
        )
        with urllib.request.urlopen(  # noqa: S310
            req, timeout=PRICING_FETCH_TIMEOUT
        ) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            return data

    @staticmethod
    def extract_dollar_amounts(text: str) -> list[float]:
        """Return all dollar-denominated numbers found in *text*."""
        return [float(m) for m in re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", text)]

    @staticmethod
    def clean_model_name(cell_html: str) -> str:
        """Strip HTML tags and footnote markers from a model-name <td>."""
        name = re.sub(r"<[^>]+>", "", cell_html).strip()
        return re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()

    @staticmethod
    def parse_model_names(header_row: str) -> list[str]:
        """Extract canonical model names from the table's header row."""
        cells = re.findall(r"<td[^>]*>(.*?)</td>", header_row, re.DOTALL)
        models: list[str] = []
        for cell in cells[1:]:  # skip the "MODEL" label cell
            name = _DeepSeekPricing.clean_model_name(cell)
            if name:
                models.append(name)
        return models

    @staticmethod
    def fill_price_column(
        prices: dict[str, dict[str, float]],
        models: list[str],
        row_html: str,
        label: str,
        field: str,
    ) -> None:
        """Extract dollar amounts from *row_html* and assign to *field* per model."""
        if label not in row_html:
            return
        amounts = _DeepSeekPricing.extract_dollar_amounts(row_html)
        for i, model in enumerate(models):
            if i < len(amounts):
                prices[model][field] = amounts[i]

    @staticmethod
    def parse_pricing_page(page_html: str) -> dict[str, dict[str, float]]:
        """Parse the DeepSeek pricing page HTML into a flat pricing table."""
        table_m = re.search(r"<table[^>]*>(.*?)</table>", page_html, re.DOTALL)
        if not table_m:
            return {}
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(1), re.DOTALL)
        if len(rows) < MIN_MODEL_COUNT:
            return {}

        models = _DeepSeekPricing.parse_model_names(rows[0])
        if len(models) < MIN_MODEL_COUNT:
            return {}

        prices: dict[str, dict[str, float]] = {
            m: {"in": 0.0, "out": 0.0, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.0} for m in models
        }

        for row in rows:
            _DeepSeekPricing.fill_price_column(prices, models, row, "CACHE HIT", "cr")
            _DeepSeekPricing.fill_price_column(prices, models, row, "CACHE MISS", "in")
            _DeepSeekPricing.fill_price_column(prices, models, row, "OUTPUT TOKENS", "out")

        # Sanity check: every model must have a positive input price
        for model, row_data in list(prices.items()):
            if row_data["in"] <= 0:
                del prices[model]
        return prices

    @staticmethod
    def load_prices(cache_path: Path) -> dict[str, dict[str, float]]:
        """Return cached or freshly-scraped DeepSeek pricing."""
        cached: dict[str, Any] | None = None
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cached = None

        fresh_enough = (
            cached is not None
            and isinstance(cached.get("fetched_at"), (int, float))
            and (time.time() - cached["fetched_at"]) < PRICING_CACHE_TTL_SECONDS
        )

        if cached is not None and fresh_enough:
            return cached.get("prices") or {}

        try:
            page = _DeepSeekPricing.http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")
            prices = _DeepSeekPricing.parse_pricing_page(page)
        except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
            if cached is not None:
                return cached.get("prices") or {}
            return {}

        if not prices:
            if cached is not None:
                return cached.get("prices") or {}
            return {}

        # Persist the freshly-scraped table
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"fetched_at": time.time(), "prices": prices},
                    indent=2,
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

        return prices


class DeepSeekProvider(MasterKeyApprovalMixin, LiteLLMProvider):
    name = "litellm-deepseek"
    master_key_prefix: ClassVar[str] = "sk-ds-"
    secret_description: ClassVar[str] = "DeepSeek API Key"  # noqa: S105

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "DEEPSEEK_API_KEY": secrets.get("_secret_key", ""),
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []

    def _get_pricing(self) -> dict[str, dict[str, float]]:
        """Return the cached DeepSeek pricing table, scraping if stale."""
        cache_path = self._state_dir() / "pricing.json"
        return _DeepSeekPricing.load_prices(cache_path)

    # --- API key auto-approval (once per sidecar lifetime, via lifecycle hooks) ---

    def on_started(self, master_key: str) -> None:
        self._approve_master_key(master_key)

    def on_stopping(self, master_key: str) -> None:
        self._unapprove_master_key(master_key)
