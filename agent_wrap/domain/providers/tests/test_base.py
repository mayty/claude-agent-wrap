# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.providers.base."""

from __future__ import annotations

from agent_wrap.domain.providers.base import _ModelKeyMatcher


def test_exact_key_beats_date_stamped_siblings() -> None:
    keys = {
        "claude-opus-4-8",
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert _ModelKeyMatcher.best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8"


def test_newest_date_wins_without_bare_key() -> None:
    keys = {
        "claude-opus-4-8-20260514",
        "claude-opus-4-8-20260512",
    }
    assert _ModelKeyMatcher.best_prefix_key("claude-opus-4-8", keys) == "claude-opus-4-8-20260514"


def test_no_cross_model_match() -> None:
    keys = {"claude-opus-4-5", "claude-opus-4-7"}
    assert _ModelKeyMatcher.best_prefix_key("claude-opus-4-8", keys) is None


def test_longest_prefix_wins() -> None:
    keys = {"claude-opus-4", "claude-opus-4-8"}
    assert _ModelKeyMatcher.best_prefix_key("claude-opus-4-8-20260514", keys) == "claude-opus-4-8"


def test_empty_keys() -> None:
    assert _ModelKeyMatcher.best_prefix_key("claude-opus-4-8", []) is None
