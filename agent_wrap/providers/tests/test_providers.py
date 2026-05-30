# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.providers."""

from __future__ import annotations

import pytest

from agent_wrap.providers import _discover_providers, get_provider


def test_discovers_bedrock():
    registry = _discover_providers()
    assert "litellm-bedrock" in registry


def test_discovers_dashscope():
    registry = _discover_providers()
    assert "litellm-dashscope" in registry


def test_default_is_bedrock():
    p = get_provider()
    assert p.name == "litellm-bedrock"


def test_explicit_bedrock():
    p = get_provider("litellm-bedrock")
    assert p.name == "litellm-bedrock"


def test_explicit_dashscope():
    p = get_provider("litellm-dashscope")
    assert p.name == "litellm-dashscope"


def test_unknown_provider_exits():
    with pytest.raises(SystemExit):
        get_provider("nonexistent-provider")
