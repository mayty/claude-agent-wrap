# This file has been created with the assistance of an AI tool.
"""
The DeepSeek scraper against the real DeepSeek pricing page. Deselected by default.

These assert invariants, not prices: a repricing must not fail them, but a page layout
the parser no longer understands must -- including a lost peak-hours footnote, which
fails quietly by billing every request at the peak rate.
"""

from typing import TYPE_CHECKING, Any

import pytest
from bs4 import BeautifulSoup, Tag

from agent_wrap.domain.providers.constants import HTML_PARSER
from agent_wrap.domain.providers.litellm_deepseek.constants import (
    MIN_MODEL_COUNT,
    PRICING_PAGE_URL,
)
from agent_wrap.domain.providers.litellm_deepseek.provider import _DeepSeekPricing
from agent_wrap.domain.providers.pricing import PricingCache

if TYPE_CHECKING:
    from agent_wrap.domain.providers.models import PriceTable

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_scrape() -> tuple[PriceTable, dict[str, Any]]:
    return _DeepSeekPricing.scrape()


@pytest.fixture(scope="module")
def live_page() -> str:
    return PricingCache.http_get(PRICING_PAGE_URL).decode("utf-8", errors="replace")


def test_live_scrape_finds_the_models(live_scrape: tuple[PriceTable, dict[str, Any]]) -> None:
    prices, _ = live_scrape
    assert len(prices) >= MIN_MODEL_COUNT
    assert all(model.startswith("deepseek-") for model in prices)


def test_live_prices_are_ordered_sensibly(
    live_scrape: tuple[PriceTable, dict[str, Any]], subtests: pytest.Subtests
) -> None:
    prices, _ = live_scrape
    for model, row in prices.items():
        with subtests.test(msg=model):
            assert row["in"] > 0
            assert row["out"] > row["in"]
            assert 0 < row["cr"] < row["in"]


def test_live_scrape_reads_the_peak_hours(
    live_scrape: tuple[PriceTable, dict[str, Any]],
) -> None:
    _, extra = live_scrape
    hours = set(extra.get("peak_hours", []))
    assert hours
    assert hours < set(range(24))


def test_live_off_peak_is_half_of_peak(live_page: str) -> None:
    """``compute_cost`` halves the scraped peak rate for off-peak hours; the page must agree."""
    table = BeautifulSoup(live_page, HTML_PARSER).find("table")
    assert isinstance(table, Tag)

    pairs: list[tuple[list[float], list[float]]] = []
    off_peak: list[float] | None = None
    for row in table.find_all("tr"):
        text = row.get_text(" ")
        # "PEAK" is a substring of "OFF-PEAK", so check off-peak first.
        if "OFF-PEAK" in text:
            off_peak = _DeepSeekPricing.extract_dollar_amounts(text)
        elif off_peak is not None and "PEAK" in text:
            pairs.append((off_peak, _DeepSeekPricing.extract_dollar_amounts(text)))
            off_peak = None

    assert pairs
    for off, peak in pairs:
        assert off == pytest.approx([amount / 2 for amount in peak])
