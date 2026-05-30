# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-bedrock provider."""

from __future__ import annotations

from agent_wrap.providers import get_provider
from agent_wrap.providers.litellm_common import LiteLLMProvider


def test_bedrock_lock_file():
    p = get_provider("litellm-bedrock")
    assert isinstance(p, LiteLLMProvider)
    assert p.lock_file == "lock"


def test_bedrock_master_key_prefix():
    p = get_provider("litellm-bedrock")
    assert isinstance(p, LiteLLMProvider)
    assert p.master_key_prefix == "sk-aw-"


def test_bedrock_label_args():
    p = get_provider("litellm-bedrock")
    args = p.get_label_args("test-123")
    assert "--label" in args
    assert "agent-wrap.role=claude-agent" in args
    assert "--name" in args
    assert "claude-agent-test-123" in args


def test_returns_list():
    p = get_provider("litellm-bedrock")
    assert isinstance(p.get_run_args(), list)


def test_empty_before_ensure():
    p = get_provider("litellm-bedrock")
    assert p.get_run_args() == []
