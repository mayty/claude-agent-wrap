# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.providers.pricing and Provider.compute_cost."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.domain.providers.pricing import CostComputer, ModelKeyMatcher

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import Mock

    from agent_wrap.conftest import FakeProvider
    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.providers.models import Tier

_OPUS_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}


def _usage(input_tokens: int, output_tokens: int = 0) -> TokenUsage:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }


def test_exact_key_beats_date_stamped_siblings() -> None:
    keys = {
        "claude-opus-4-8",
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert ModelKeyMatcher.best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8"


def test_newest_date_wins_without_bare_key() -> None:
    keys = {
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert ModelKeyMatcher.best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8-20260514"


def test_no_cross_model_match() -> None:
    keys = {"claude-opus-4-5", "claude-opus-4-7"}
    assert ModelKeyMatcher.best_prefix_key("claude-opus-4-8", keys) is None


def test_longest_prefix_wins() -> None:
    keys = {"claude-opus-4", "claude-opus-4-8"}
    assert ModelKeyMatcher.best_prefix_key("claude-opus-4-8-20260514", keys) == "claude-opus-4-8"


def test_empty_keys() -> None:
    assert ModelKeyMatcher.best_prefix_key("claude-opus-4-8", []) is None


def test_worst_case_cost_empty_table_is_zero() -> None:
    assert CostComputer.worst_case_cost({}, _usage(1000, 500)) == 0.0


def test_worst_case_cost_single_flat_tier() -> None:
    table: dict[str, list[Tier]] = {
        "claude-opus-4-8": [
            {"max_in": float("inf"), "in_": 5.5, "out": 27.5, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.0}
        ]
    }
    usage = _usage(1000, 500)
    # 1000 * 5.5/1M + 500 * 27.5/1M = 0.0055 + 0.01375 = 0.01925
    assert CostComputer.worst_case_cost(table, usage) == 0.01925


def test_worst_case_cost_picks_max_across_models_and_tiers() -> None:
    table: dict[str, list[Tier]] = {
        "cheap-model": [
            {"max_in": float("inf"), "in_": 1.0, "out": 1.0, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.0}
        ],
        "tiered-model": [
            {
                "max_in": 200_000.0,
                "in_": 5.0,
                "out": 5.0,
                "cw_5m": 0.0,
                "cw_1h": 0.0,
                "cr": 0.0,
            },
            {
                "max_in": float("inf"),
                "in_": 50.0,
                "out": 50.0,
                "cw_5m": 0.0,
                "cw_1h": 0.0,
                "cr": 0.0,
            },
        ],
    }
    usage = _usage(1000, 0)
    # Highest per-tier rate is the tiered model's second tier (50.0/1M in).
    assert CostComputer.worst_case_cost(table, usage) == 1000 * 50.0 / 1_000_000


def test_compute_cost_unknown_model_negligible_usage_returns_zero(
    display_mock: Mock, make_fake_provider: Callable[..., FakeProvider]
) -> None:
    provider = make_fake_provider(display_mock, flat={"claude-opus-4-8": _OPUS_RATES})
    # A handful of tokens against Opus 4.8 rates rounds down to $0.
    cost = provider.compute_cost("claude-unknown-model", _usage(10, 5))
    assert cost == 0.0


def test_compute_cost_unknown_model_non_negligible_usage_returns_none(
    display_mock: Mock, make_fake_provider: Callable[..., FakeProvider]
) -> None:
    provider = make_fake_provider(display_mock, flat={"claude-opus-4-8": _OPUS_RATES})
    cost = provider.compute_cost("claude-unknown-model", _usage(1000, 500))
    assert cost is None


def test_compute_cost_no_pricing_table_returns_none(
    display_mock: Mock, make_fake_provider: Callable[..., FakeProvider]
) -> None:
    provider = make_fake_provider(display_mock, flat={})
    assert provider.compute_cost("claude-opus-4-8", _usage(10, 5)) is None
