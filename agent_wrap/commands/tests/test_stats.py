# This file has been created with the assistance of an AI tool.
"""Tests for the `stats` subcommand's model→pricing matching."""

from __future__ import annotations

from agent_wrap.commands.stats import PriceSource, _best_prefix_key

# --- _best_prefix_key ---


def test_exact_key_beats_date_stamped_siblings():
    # Shortest-name tie-break: the bare key wins over date-stamped variants.
    keys = {
        "claude-opus-4-8",
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert _best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8"


def test_newest_date_wins_without_bare_key():
    # Alphabetic-desc tie-break among equal prefixes: newer date suffix wins.
    keys = {
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert _best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8-20260514"


def test_no_cross_model_match():
    # Neither string is a prefix of the other, so distinct models never match.
    keys = {"claude-opus-4-5", "claude-opus-4-7"}
    assert _best_prefix_key("claude-opus-4-8", keys) is None


def test_longest_prefix_wins():
    # A date-stamped request resolves to the most specific base key available.
    keys = {"claude-opus-4", "claude-opus-4-8"}
    assert _best_prefix_key("claude-opus-4-8-20260514", keys) == "claude-opus-4-8"


def test_empty_keys():
    assert _best_prefix_key("claude-opus-4-8", []) is None


# --- PriceSource round-trip ---


class _FakeProvider:
    def __init__(self, flat=None, tiered=None):
        self._flat = flat or {}
        self._tiered = tiered

    def get_pricing(self):
        return self._flat

    def get_tiered_pricing(self):
        return self._tiered


def test_date_stamped_request_resolves_to_base_tier(monkeypatch):
    rates = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}
    fake = _FakeProvider(flat={"claude-opus-4-8": rates})
    monkeypatch.setattr("agent_wrap.commands.stats.get_provider", lambda name: fake)

    prices = PriceSource()
    tiers = prices.get_pricing("bedrock", "us.anthropic.claude-opus-4-8-20260514")

    assert tiers is not None
    assert len(tiers) == 1
    assert tiers[0]["in"] == 5.5
    assert tiers[0]["max_in"] == float("inf")


def test_unknown_model_returns_none(monkeypatch):
    rates = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}
    fake = _FakeProvider(flat={"claude-opus-4-8": rates})
    monkeypatch.setattr("agent_wrap.commands.stats.get_provider", lambda name: fake)

    prices = PriceSource()
    assert prices.get_pricing("bedrock", "claude-opus-4-5") is None
