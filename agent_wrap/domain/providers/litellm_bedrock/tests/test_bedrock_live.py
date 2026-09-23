# This file has been created with the assistance of an AI tool.
"""
The Bedrock scraper against the real AWS pricing page. Deselected by default.

These assert invariants, not prices: a repricing must not fail them, but a page layout
the parser no longer understands must -- it yields an empty or column-shifted table.
"""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.providers.litellm_bedrock.provider import _BedrockPricing

if TYPE_CHECKING:
    from agent_wrap.domain.providers.models import PriceTable

pytestmark = pytest.mark.live

# Models on sale today. Remove one only once AWS stops listing it.
_EXPECTED_MODELS = ("claude-opus-4-5", "claude-sonnet-4-5", "claude-haiku-4-5", "claude-opus-5-5")


@pytest.fixture(scope="module")
def live_prices() -> PriceTable:
    prices, _ = _BedrockPricing.scrape()
    return prices


def test_live_scrape_finds_every_family(live_prices: PriceTable) -> None:
    families = {key.split("-")[1] for key in live_prices}
    assert {"opus", "sonnet", "haiku"} <= families


def test_live_scrape_finds_the_current_models(live_prices: PriceTable) -> None:
    assert set(_EXPECTED_MODELS) <= set(live_prices)


def test_live_prices_are_ordered_sensibly(
    live_prices: PriceTable, subtests: pytest.Subtests
) -> None:
    for model, row in live_prices.items():
        with subtests.test(msg=model):
            assert set(row) == {"in", "out", "cw_5m", "cw_1h", "cr"}
            assert all(price > 0 for price in row.values())
            assert row["out"] > row["in"]
            assert row["cr"] < row["in"]


def test_live_prices_keep_the_cache_write_multipliers(
    live_prices: PriceTable, subtests: pytest.Subtests
) -> None:
    """Cache writes cost 1.25x (5m) and 2x (1h) input; a shifted column breaks both."""
    for model, row in live_prices.items():
        with subtests.test(msg=model):
            assert row["cw_5m"] == pytest.approx(1.25 * row["in"])
            assert row["cw_1h"] == pytest.approx(2 * row["in"])
