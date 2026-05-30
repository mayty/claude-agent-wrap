# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.providers."""

from __future__ import annotations

import inspect

import pytest

from agent_wrap.providers import _discover_providers, get_provider
from agent_wrap.providers.base import Provider
from agent_wrap.providers.litellm_common import LiteLLMProvider


class TestProviderDiscovery:
    def test_discovers_bedrock(self):
        registry = _discover_providers()
        assert "litellm-bedrock" in registry

    def test_discovers_dashscope(self):
        registry = _discover_providers()
        assert "litellm-dashscope" in registry

    def test_excludes_abstract_classes(self):
        registry = _discover_providers()
        for name, cls in registry.items():
            assert not inspect.isabstract(cls), f"{name} is abstract"

    def test_exactly_two_providers(self):
        registry = _discover_providers()
        assert len(registry) == 2


class TestGetProvider:
    def test_default_is_bedrock(self):
        p = get_provider()
        assert p.name == "litellm-bedrock"

    def test_explicit_bedrock(self):
        p = get_provider("litellm-bedrock")
        assert p.name == "litellm-bedrock"

    def test_explicit_dashscope(self):
        p = get_provider("litellm-dashscope")
        assert p.name == "litellm-dashscope"

    def test_unknown_provider_exits(self):
        with pytest.raises(SystemExit):
            get_provider("nonexistent-provider")


class TestProviderInterface:
    def test_bedrock_implements_all_methods(self):
        p = get_provider("litellm-bedrock")
        assert hasattr(p, "ensure")
        assert hasattr(p, "release")
        assert hasattr(p, "get_run_args")
        assert hasattr(p, "get_label_args")

    def test_provider_is_abstract(self):
        assert inspect.isabstract(Provider)

    def test_litellm_provider_is_abstract(self):
        assert inspect.isabstract(LiteLLMProvider)


class TestProviderAttributes:
    def test_bedrock_lock_file(self):
        p = get_provider("litellm-bedrock")
        assert p.lock_file == "lock"  # pyrefly: ignore[missing-attribute]

    def test_dashscope_lock_file(self):
        p = get_provider("litellm-dashscope")
        assert p.lock_file == "lock"  # pyrefly: ignore[missing-attribute]

    def test_bedrock_master_key_prefix(self):
        p = get_provider("litellm-bedrock")
        assert p.master_key_prefix == "sk-aw-"  # pyrefly: ignore[missing-attribute]

    def test_dashscope_master_key_prefix(self):
        p = get_provider("litellm-dashscope")
        assert p.master_key_prefix == "sk-ds-"  # pyrefly: ignore[missing-attribute]


class TestLabelArgs:
    def test_bedrock_label_args(self):
        p = get_provider("litellm-bedrock")
        args = p.get_label_args("test-123")
        assert "--label" in args
        assert "agent-wrap.role=claude-agent" in args
        assert "--name" in args
        assert "claude-agent-test-123" in args

    def test_empty_instance_id(self):
        p = get_provider("litellm-bedrock")
        assert p.get_label_args("") == []


class TestGetRunArgs:
    def test_returns_list(self):
        p = get_provider("litellm-bedrock")
        assert isinstance(p.get_run_args(), list)

    def test_empty_before_ensure(self):
        p = get_provider("litellm-bedrock")
        assert p.get_run_args() == []
