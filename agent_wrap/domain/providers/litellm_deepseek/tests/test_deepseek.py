# This file has been created with the assistance of an AI tool.
"""Tests for the litellm-deepseek provider pricing cache."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from agent_wrap.domain.providers.litellm_deepseek.provider import _DeepSeekPricing

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock

# A page with the header row (MODEL + two models) and the three price rows the
# parser looks for.
_PAGE_HTML = (
    "<table>"
    "<tr><td>MODEL</td><td>deepseek-v4-pro</td><td>deepseek-v4-flash</td></tr>"
    "<tr><td>CACHE HIT</td><td>$0.30</td><td>$0.05</td></tr>"
    "<tr><td>CACHE MISS</td><td>$3.00</td><td>$0.50</td></tr>"
    "<tr><td>OUTPUT TOKENS</td><td>$15.00</td><td>$2.00</td></tr>"
    "</table>"
)


def _fresh_cache(tmp_path: Path) -> Path:
    """Write a brand-new pricing cache; return its path."""
    cache_path = tmp_path / "pricing.json"
    cache_path.write_text(
        json.dumps({"fetched_at": time.time(), "prices": {"deepseek-v4-pro": {"in": 1.0}}}),
        encoding="utf-8",
    )
    return cache_path


def test_load_prices_serves_fresh_cache_without_fetching(
    tmp_path: Path, mocker: pytest_mock.MockFixture
):
    cache_path = _fresh_cache(tmp_path)
    http_get = mocker.patch.object(_DeepSeekPricing, "http_get", autospec=True)

    prices = _DeepSeekPricing.load_prices(cache_path)

    assert prices == {"deepseek-v4-pro": {"in": 1.0}}
    http_get.assert_not_called()


def test_load_prices_force_refetches_fresh_cache(tmp_path: Path, mocker: pytest_mock.MockFixture):
    """``refresh_pricing_data=True`` bypasses even a brand-new cache and re-fetches."""
    cache_path = _fresh_cache(tmp_path)
    http_get = mocker.patch.object(_DeepSeekPricing, "http_get", autospec=True)
    http_get.return_value = _PAGE_HTML.encode()

    prices = _DeepSeekPricing.load_prices(cache_path, refresh_pricing_data=True)

    # Freshly parsed from the mocked page, not the cached placeholder row.
    assert prices["deepseek-v4-pro"]["in"] == 3.0
    assert "deepseek-v4-pro" in prices
    assert prices["deepseek-v4-flash"]["cr"] == 0.05
    http_get.assert_called_once()
