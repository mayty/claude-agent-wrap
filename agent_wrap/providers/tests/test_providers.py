# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.providers."""

from __future__ import annotations

import inspect

import pytest

from agent_wrap.providers import _discover_providers, get_provider
from agent_wrap.providers.base import Provider
from agent_wrap.providers.litellm_common import LiteLLMProvider


def test_discovers_bedrock():
    registry = _discover_providers()
    assert "litellm-bedrock" in registry


def test_discovers_dashscope():
    registry = _discover_providers()
    assert "litellm-dashscope" in registry


def test_excludes_abstract_classes():
    registry = _discover_providers()
    for name, cls in registry.items():
        assert not inspect.isabstract(cls), f"{name} is abstract"


def test_exactly_two_providers():
    registry = _discover_providers()
    assert len(registry) == 2


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


def test_bedrock_implements_all_methods():
    p = get_provider("litellm-bedrock")
    assert hasattr(p, "ensure")
    assert hasattr(p, "release")
    assert hasattr(p, "get_run_args")
    assert hasattr(p, "get_label_args")


def test_provider_is_abstract():
    assert inspect.isabstract(Provider)


def test_litellm_provider_is_abstract():
    assert inspect.isabstract(LiteLLMProvider)
