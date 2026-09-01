# This file has been created with the assistance of an AI tool.
"""Tests for the litellm-deepseek provider."""

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers import litellm_deepseek as provider_module
from agent_wrap.domain.providers.litellm_deepseek.provider import (
    DeepSeekProvider,
    _DeepSeekPricing,
)
from agent_wrap.domain.sidecars.service import SidecarService

if TYPE_CHECKING:
    import pytest_mock

    from agent_wrap.domain.pricing.models import TokenUsage

# A page matching the current two-column layout: each metric is a paired
# OFF-PEAK / PEAK pair of rows (the OFF-PEAK row carries the metric label in a
# rowspan cell), plus the peak-hours footnote. OFF-PEAK and PEAK values are
# deliberately distinct so the parser's peak-selection is asserted, not assumed.
_PAGE_HTML = (
    "<table>"
    '<tr><td colspan="3">MODEL</td><td>deepseek-v4-pro</td><td>deepseek-v4-flash</td></tr>'
    '<tr><td rowspan="2">1M INPUT TOKENS (CACHE HIT)</td><td>OFF-PEAK</td><td>$1.00</td><td>$2.00</td></tr>'
    "<tr><td>PEAK</td><td>$3.00</td><td>$4.00</td></tr>"
    '<tr><td rowspan="2">1M INPUT TOKENS (CACHE MISS)</td><td>OFF-PEAK</td><td>$5.00</td><td>$6.00</td></tr>'
    "<tr><td>PEAK</td><td>$7.00</td><td>$8.00</td></tr>"
    '<tr><td rowspan="2">1M OUTPUT TOKENS</td><td>OFF-PEAK</td><td>$9.00</td><td>$10.00</td></tr>'
    "<tr><td>PEAK</td><td>$11.00</td><td>$12.00</td></tr>"
    "</table>"
    "<p>(1) Off-peak rates are half of the peak rates. Peak hours are "
    "01:00 - 04:00 and 06:00 - 10:00 UTC (all other hours are off-peak).</p>"
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

    # Peak (not off-peak) values parsed from the mocked page.
    assert prices["deepseek-v4-pro"]["in"] == 7.0
    assert prices["deepseek-v4-flash"]["cr"] == 4.0
    http_get.assert_called_once()


def test_parse_pricing_page_selects_peak_rates():
    """The two-column page must yield the PEAK column, not the OFF-PEAK one."""
    prices = _DeepSeekPricing.parse_pricing_page(_PAGE_HTML)

    assert prices["deepseek-v4-pro"] == {
        "in": 7.0,
        "out": 11.0,
        "cw_5m": 0.0,
        "cw_1h": 0.0,
        "cr": 3.0,
    }
    assert prices["deepseek-v4-flash"] == {
        "in": 8.0,
        "out": 12.0,
        "cw_5m": 0.0,
        "cw_1h": 0.0,
        "cr": 4.0,
    }


def test_extract_peak_hours_parses_footnote():
    """``01:00 - 04:00`` and ``06:00 - 10:00`` UTC expand to hours 1-3 and 6-9."""
    hours = _DeepSeekPricing.extract_peak_hours(_PAGE_HTML)

    assert hours == frozenset({1, 2, 3, 6, 7, 8, 9})


def test_extract_peak_hours_returns_none_without_footnote():
    assert _DeepSeekPricing.extract_peak_hours("<p>no peak hours here</p>") is None


def test_load_prices_persists_peak_hours(tmp_path: Path, mocker: pytest_mock.MockFixture):
    cache_path = _fresh_cache(tmp_path)
    http_get = mocker.patch.object(_DeepSeekPricing, "http_get", autospec=True)
    http_get.return_value = _PAGE_HTML.encode()

    _DeepSeekPricing.load_prices(cache_path, refresh_pricing_data=True)

    doc = json.loads(cache_path.read_text(encoding="utf-8"))
    assert doc["peak_hours"] == [1, 2, 3, 6, 7, 8, 9]
    assert doc["prices"]["deepseek-v4-pro"]["in"] == 7.0


def test_load_peak_hours_returns_set_when_present(tmp_path: Path):
    cache_path = tmp_path / "pricing.json"
    cache_path.write_text(
        json.dumps({"fetched_at": 1, "peak_hours": [6, 7], "prices": {}}),
        encoding="utf-8",
    )

    assert _DeepSeekPricing.load_peak_hours(cache_path) == frozenset({6, 7})


def test_load_peak_hours_returns_none_when_absent_or_malformed(tmp_path: Path):
    missing = tmp_path / "missing.json"
    assert _DeepSeekPricing.load_peak_hours(missing) is None

    no_key = tmp_path / "no_key.json"
    no_key.write_text(json.dumps({"fetched_at": 1, "prices": {}}), encoding="utf-8")
    assert _DeepSeekPricing.load_peak_hours(no_key) is None

    malformed = tmp_path / "malformed.json"
    malformed.write_text(
        json.dumps({"fetched_at": 1, "peak_hours": ["x"], "prices": {}}),
        encoding="utf-8",
    )
    assert _DeepSeekPricing.load_peak_hours(malformed) is None


@pytest.fixture
def deepseek() -> DeepSeekProvider:
    """Return a DeepSeekProvider with no-op sidecar."""
    return DeepSeekProvider(
        sidecar_service=Mock(spec=SidecarService),
        display_service=Mock(spec=DisplayService),
    )


def _usage() -> TokenUsage:
    return {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 0,
            "ephemeral_1h_input_tokens": 0,
        },
    }


def _mock_pricing(mocker: pytest_mock.MockFixture, *, peak_hours: frozenset[int] | None) -> None:
    """Stub the scraper so compute_cost is deterministic and network-free."""
    mocker.patch.object(
        _DeepSeekPricing,
        "load_prices",
        return_value={
            "deepseek-v4-pro": {"in": 1.0, "out": 2.0, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.1}
        },
    )
    mocker.patch.object(_DeepSeekPricing, "load_peak_hours", return_value=peak_hours)


def test_compute_cost_charges_peak_rate_during_peak_hour(
    deepseek: DeepSeekProvider, mocker: pytest_mock.MockFixture
):
    _mock_pricing(mocker, peak_hours=frozenset({6, 7}))

    # Monday (weekday=0) 06:00 UTC is a peak hour.
    assert deepseek.compute_cost("deepseek-v4-pro[1m]", _usage(), hour=6, weekday=0) == 3.0


def test_compute_cost_halves_off_peak_hour(
    deepseek: DeepSeekProvider, mocker: pytest_mock.MockFixture
):
    _mock_pricing(mocker, peak_hours=frozenset({6, 7}))

    # Monday 05:00 UTC is off-peak.
    assert deepseek.compute_cost("deepseek-v4-pro[1m]", _usage(), hour=5, weekday=0) == 1.5


def test_compute_cost_halves_weekend_peak_hour(
    deepseek: DeepSeekProvider, mocker: pytest_mock.MockFixture
):
    _mock_pricing(mocker, peak_hours=frozenset({6, 7}))

    # Saturday (weekday=5) 06:00 UTC is off-peak even at a peak hour.
    assert deepseek.compute_cost("deepseek-v4-pro[1m]", _usage(), hour=6, weekday=5) == 1.5


def test_compute_cost_unknown_hour_charges_peak(
    deepseek: DeepSeekProvider, mocker: pytest_mock.MockFixture
):
    _mock_pricing(mocker, peak_hours=frozenset({6, 7}))

    assert deepseek.compute_cost("deepseek-v4-pro[1m]", _usage(), hour=None, weekday=0) == 3.0


def test_compute_cost_unknown_weekday_charges_peak(
    deepseek: DeepSeekProvider, mocker: pytest_mock.MockFixture
):
    _mock_pricing(mocker, peak_hours=frozenset({6, 7}))

    assert deepseek.compute_cost("deepseek-v4-pro[1m]", _usage(), hour=6, weekday=None) == 3.0


def test_compute_cost_unknown_peak_hours_charges_peak(
    deepseek: DeepSeekProvider, mocker: pytest_mock.MockFixture
):
    _mock_pricing(mocker, peak_hours=None)

    assert deepseek.compute_cost("deepseek-v4-pro[1m]", _usage(), hour=5, weekday=0) == 3.0


def test_deepseek_config_targets_the_anthropic_compatible_upstream():
    """
    DeepSeek is reached through its Anthropic-compatible endpoint, not its OpenAI one, so
    Claude Code's own `/v1/messages` traffic needs no translation beyond LiteLLM's.
    """
    config_path = Path(provider_module.__file__).parent / "config.yaml"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    text = "\n".join(lines)
    assert 'model: "anthropic/*"' in text
    assert "api_base: https://api.deepseek.com/anthropic" in text
