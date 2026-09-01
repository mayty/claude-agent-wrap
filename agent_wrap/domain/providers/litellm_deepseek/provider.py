# This file has been edited with the assistance of an AI tool.
"""LiteLLM DeepSeek provider — routes Claude Code through DeepSeek provider."""

import gzip
import json
import re
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, ClassVar, override

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.key_approval import MasterKeyApprovalMixin
from agent_wrap.domain.providers.litellm_deepseek.constants import (
    MIN_MODEL_COUNT,
    PEAK_WEEKDAYS,
    PRICING_CACHE_TTL_SECONDS,
    PRICING_FETCH_TIMEOUT,
    PRICING_PAGE_URL,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.pricing.models import TokenUsage


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
    def _metric_field(row_html: str) -> str | None:
        """Map a pricing-row metric label to its flat-table field, or None."""
        if "CACHE HIT" in row_html:
            return "cr"
        if "CACHE MISS" in row_html:
            return "in"
        if "OUTPUT" in row_html:
            return "out"
        return None

    @staticmethod
    def _extract_peak_prices(models: list[str], rows: list[str]) -> dict[str, dict[str, float]]:
        """Fill *models*' peak rates from the OFF-PEAK/PEAK row pairs in *rows*."""
        prices: dict[str, dict[str, float]] = {
            m: {"in": 0.0, "out": 0.0, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.0} for m in models
        }

        # "PEAK" is a substring of "OFF-PEAK", so check off-peak first.
        current_field: str | None = None
        for row in rows:
            if "OFF-PEAK" in row:
                current_field = _DeepSeekPricing._metric_field(row)
            elif current_field is not None and "PEAK" in row:
                amounts = _DeepSeekPricing.extract_dollar_amounts(row)
                for i, model in enumerate(models):
                    if i < len(amounts):
                        prices[model][current_field] = amounts[i]
                current_field = None
        return prices

    @staticmethod
    def parse_pricing_page(page_html: str) -> dict[str, dict[str, float]]:
        """
        Parse the DeepSeek pricing page HTML into a flat **peak-rate** table.

        Each metric is priced in a paired OFF-PEAK / PEAK pair of rows: the
        OFF-PEAK row carries the metric label (in a ``rowspan`` cell) and the PEAK
        row that follows it carries only the peak dollar amounts. Only the PEAK
        amounts are captured — off-peak is exactly half of peak, which the provider
        derives at cost time.
        """
        table_m = re.search(r"<table[^>]*>(.*?)</table>", page_html, re.DOTALL)
        if not table_m:
            return {}
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_m.group(1), re.DOTALL)
        if len(rows) < MIN_MODEL_COUNT:
            return {}

        models = _DeepSeekPricing.parse_model_names(rows[0])
        if len(models) < MIN_MODEL_COUNT:
            return {}

        prices = _DeepSeekPricing._extract_peak_prices(models, rows)

        # Sanity check: every model must have a positive input price
        for model, row_data in list(prices.items()):
            if row_data["in"] <= 0:
                del prices[model]
        return prices

    @staticmethod
    def extract_peak_hours(page_html: str) -> frozenset[int] | None:
        """
        Parse the peak-hours footnote into a set of UTC hours, or None.

        The page's footnote reads e.g. "Peak hours are 01:00 - 04:00 and
        06:00 - 10:00 UTC", which expands to the half-open intervals ``[1, 4)``
        and ``[6, 10)`` — the hours ``{1, 2, 3, 6, 7, 8, 9}``. Returns None when
        the footnote or any time range is absent, so callers can fall back to
        charging peak rates.
        """
        m = re.search(r"Peak hours are\s+([^.]*?)\s*UTC", page_html)
        if not m:
            return None
        ranges = re.findall(r"(\d{1,2}):\d{2}\s*-\s*(\d{1,2}):\d{2}", m.group(1))
        if not ranges:
            return None
        hours: set[int] = set()
        for start, end in ranges:
            hours.update(range(int(start), int(end)))
        return frozenset(hours)

    @staticmethod
    def load_prices(
        cache_path: Path, *, refresh_pricing_data: bool = False
    ) -> dict[str, dict[str, float]]:
        """Return cached or freshly-scraped DeepSeek pricing (peak rates)."""
        cached: dict[str, Any] | None = None
        if cache_path.is_file():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
            except OSError, json.JSONDecodeError:
                cached = None

        fresh_enough = (
            cached is not None
            and isinstance(cached.get("fetched_at"), (int, float))
            and (time.time() - cached["fetched_at"]) < PRICING_CACHE_TTL_SECONDS
        )

        if not refresh_pricing_data and cached is not None and fresh_enough:
            return cached.get("prices") or {}

        try:
            page = _DeepSeekPricing.http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")
            prices = _DeepSeekPricing.parse_pricing_page(page)
            peak_hours = _DeepSeekPricing.extract_peak_hours(page)
        except urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError:
            if cached is not None:
                return cached.get("prices") or {}
            return {}

        if not prices:
            if cached is not None:
                return cached.get("prices") or {}
            return {}

        # Persist the freshly-scraped table (plus peak hours, when the page says)
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            doc: dict[str, Any] = {"fetched_at": time.time(), "prices": prices}
            if peak_hours is not None:
                doc["peak_hours"] = sorted(peak_hours)
            cache_path.write_text(
                json.dumps(doc, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            pass

        return prices

    @staticmethod
    def load_peak_hours(cache_path: Path) -> frozenset[int] | None:
        """Return the cached peak hours (UTC), or None when unknown or absent."""
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None
        hours = cached.get("peak_hours")
        if not isinstance(hours, list):
            return None
        try:
            return frozenset(int(hour) for hour in hours)
        except TypeError, ValueError:
            return None


class DeepSeekProvider(MasterKeyApprovalMixin, Provider):
    name = "litellm-deepseek"
    master_key_prefix: ClassVar[str] = "sk-ds-"
    secret_description: ClassVar[str] = "DeepSeek API Key"  # noqa: S105

    @override
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "DEEPSEEK_API_KEY": secrets.get("api_key", ""),
        }

    @override
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": "deepseek-v4-flash-vision-exp[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-flash-vision-exp[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash-vision-exp[1m]",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash-vision-exp[1m]",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
        }

    @override
    def _get_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, dict[str, float]]:
        """Return the cached DeepSeek pricing table, scraping if stale."""
        cache_path = self._state_dir() / "pricing.json"
        return _DeepSeekPricing.load_prices(cache_path, refresh_pricing_data=refresh_pricing_data)

    @override
    def compute_cost(
        self,
        model: str,
        usage: TokenUsage,
        *,
        hour: int | None,
        weekday: int | None = None,
        refresh_pricing_data: bool = False,
    ) -> float | None:
        """
        Compute cost at peak rates, halved for off-peak or weekend usage.

        The scraped table holds DeepSeek's peak-hour rates; off-peak is exactly
        half. Peak rates apply Monday through Friday (``PEAK_WEEKDAYS``) during
        the peak hours — weekends are off-peak all day. When *hour* or *weekday*
        is None (timestamp unknown) or the peak-hours list is unknown, the peak
        rate is charged — never under-report.
        """
        cost = super().compute_cost(
            model, usage, hour=hour, refresh_pricing_data=refresh_pricing_data
        )
        if cost is None or hour is None or weekday is None:
            return cost
        peak_hours = _DeepSeekPricing.load_peak_hours(self._state_dir() / "pricing.json")
        if peak_hours is None:
            return cost
        is_peak = weekday in PEAK_WEEKDAYS and hour in peak_hours
        return cost if is_peak else cost / 2

    # --- API key auto-approval (once per sidecar lifetime, via lifecycle hooks) ---

    @override
    def on_started(self, master_key: str) -> None:
        self._approve_master_key(master_key)

    @override
    def on_stopping(self, master_key: str) -> None:
        self._unapprove_master_key(master_key)
