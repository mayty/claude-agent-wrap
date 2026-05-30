# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-dashscope provider."""

from __future__ import annotations

from agent_wrap.providers import get_provider
from agent_wrap.providers.litellm_common import LiteLLMProvider


def test_dashscope_lock_file():
    p = get_provider("litellm-dashscope")
    assert isinstance(p, LiteLLMProvider)
    assert p.lock_file == "lock"


def test_dashscope_master_key_prefix():
    p = get_provider("litellm-dashscope")
    assert isinstance(p, LiteLLMProvider)
    assert p.master_key_prefix == "sk-ds-"


def test_empty_instance_id():
    p = get_provider("litellm-dashscope")
    assert p.get_label_args("") == []
