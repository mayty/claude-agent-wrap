# This file has been edited with the assistance of an AI tool.
"""LiteLLM DeepSeek provider — routes Claude Code through DeepSeek provider."""

import json
import re
from typing import TYPE_CHECKING, Any, ClassVar, override

from bs4 import BeautifulSoup, Tag

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.constants import HTML_PARSER
from agent_wrap.domain.providers.key_approval import MasterKeyApprovalMixin
from agent_wrap.domain.providers.litellm_deepseek.constants import (
    FOOTNOTE_SUFFIX_RE,
    MIN_MODEL_COUNT,
    PEAK_HOURS_RE,
    PEAK_WEEKDAYS,
    PRICING_PAGE_URL,
    TIME_RANGE_RE,
)
from agent_wrap.domain.providers.pricing import PricingCache

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.providers.models import PriceTable


class _DeepSeekPricing:
    """Scrapes the official DeepSeek pricing page and caches for 7 days."""

    @staticmethod
    def extract_dollar_amounts(text: str) -> list[float]:
        return [float(m) for m in re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", text)]

    @staticmethod
    def clean_model_name(cell_text: str) -> str:
        return FOOTNOTE_SUFFIX_RE.sub("", cell_text.strip()).strip()

    @staticmethod
    def parse_model_names(header_row: Tag) -> list[str]:
        cells = header_row.find_all("td")
        models: list[str] = []
        for cell in cells[1:]:  # skip the "MODEL" label cell
            name = _DeepSeekPricing.clean_model_name(cell.get_text())
            if name:
                models.append(name)
        return models

    @staticmethod
    def _metric_field(row_text: str) -> str | None:
        """Map a pricing-row metric label to its flat-table field, or None."""
        if "CACHE HIT" in row_text:
            return "cr"
        if "CACHE MISS" in row_text:
            return "in"
        if "OUTPUT" in row_text:
            return "out"
        return None

    @staticmethod
    def _extract_peak_prices(models: list[str], rows: list[Tag]) -> dict[str, dict[str, float]]:
        prices: dict[str, dict[str, float]] = {
            m: {"in": 0.0, "out": 0.0, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.0} for m in models
        }

        # "PEAK" is a substring of "OFF-PEAK", so check off-peak first.
        current_field: str | None = None
        for row in rows:
            text = row.get_text(" ")
            if "OFF-PEAK" in text:
                current_field = _DeepSeekPricing._metric_field(text)
            elif current_field is not None and "PEAK" in text:
                amounts = _DeepSeekPricing.extract_dollar_amounts(text)
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
        table = BeautifulSoup(page_html, HTML_PARSER).find("table")
        if not isinstance(table, Tag):
            return {}
        rows = table.find_all("tr")
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

        Read from the page's *text*: the sentence is prose, and a tag boundary
        anywhere inside it would hide it from a match against the markup.
        """
        text = BeautifulSoup(page_html, HTML_PARSER).get_text(" ")
        m = PEAK_HOURS_RE.search(text)
        if not m:
            return None
        ranges = TIME_RANGE_RE.findall(m.group(1))
        if not ranges:
            return None
        hours: set[int] = set()
        for start, end in ranges:
            hours.update(range(int(start), int(end)))
        return frozenset(hours)

    @staticmethod
    def scrape() -> tuple[PriceTable, dict[str, Any]]:
        """
        Scrape the peak-rate table, and the peak-hours footnote when the page states one.

        The hours ride along in the cached document because ``compute_cost`` needs them
        per request and must not re-fetch the page to find out which rate applies.
        """
        page = PricingCache.http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")
        prices = _DeepSeekPricing.parse_pricing_page(page)
        peak_hours = _DeepSeekPricing.extract_peak_hours(page)
        extra = {} if peak_hours is None else {"peak_hours": sorted(peak_hours)}
        return prices, extra

    @staticmethod
    def load_prices(
        cache_path: Path,
        display: DisplayService,
        provider: str,
        *,
        refresh_pricing_data: bool = False,
    ) -> PriceTable:
        return PricingCache.load(
            cache_path,
            display,
            provider,
            refresh=refresh_pricing_data,
            scrape=_DeepSeekPricing.scrape,
        )

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
            "ANTHROPIC_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-flash[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-flash[1m]",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-flash[1m]",
            # Explore's built-in "inherit" guard caps it at Opus whenever the session model's name
            # lacks a haiku/sonnet/opus token, true for every non-Anthropic ID. Disable it so
            # Explore tracks the session's running model, not CLAUDE_CODE_SUBAGENT_MODEL.
            "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
            # The sidecar is a gateway, so the server's auto-mode classifier checks never reach the
            # session; opt out instead of stopping the first checked action on the ineligibility notice.
            "CLAUDE_CODE_AUTO_MODE_SERVER": "0",
        }

    @override
    def _get_pricing(self, *, refresh_pricing_data: bool = False) -> PriceTable:
        return _DeepSeekPricing.load_prices(
            self._pricing_cache_path(),
            self._display,
            self.name,
            refresh_pricing_data=refresh_pricing_data,
        )

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
        peak_hours = _DeepSeekPricing.load_peak_hours(self._pricing_cache_path())
        if peak_hours is None:
            return cost
        is_peak = weekday in PEAK_WEEKDAYS and hour in peak_hours
        return cost if is_peak else cost / 2

    @override
    def on_started(self, master_key: str) -> None:
        self._approve_master_key(master_key)

    @override
    def on_stopping(self, master_key: str) -> None:
        self._unapprove_master_key(master_key)
