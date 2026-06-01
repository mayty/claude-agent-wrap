# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-bedrock provider."""

from __future__ import annotations

from agent_wrap.providers import get_provider
from agent_wrap.providers.litellm_common import LiteLLMProvider


def _bedrock() -> LiteLLMProvider:
    p = get_provider("litellm-bedrock")
    assert isinstance(p, LiteLLMProvider)
    return p


def test_bedrock_lock_file():
    p = _bedrock()
    assert p.lock_file == "lock"


def test_bedrock_master_key_prefix():
    p = _bedrock()
    assert p.master_key_prefix == "sk-aw-"


def test_bedrock_label_args():
    p = _bedrock()
    args = p.get_label_args("test-123")
    assert "--label" in args
    assert "agent-wrap.role=claude-agent" in args
    assert "--name" in args
    assert "claude-agent-test-123" in args


# --- Provider method implementations ---


def test_bedrock_get_sidecar_env():
    p = _bedrock()
    env = p.get_sidecar_env({"_secret_key": "my-aws-key"})
    assert "AWS_BEARER_TOKEN_BEDROCK" in env
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == "my-aws-key"


def test_bedrock_get_agent_env():
    p = _bedrock()
    env = p.get_agent_env("sk-aw-abc123", "http://proxy:4000/bedrock")
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == "sk-aw-abc123"
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://proxy:4000/bedrock/bedrock"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "us-east-1"


def test_bedrock_read_secret_key():
    p = _bedrock()
    key = p.read_secret_key({"ServiceSpecificCredential": {"ServiceCredentialSecret": "aws-key"}})
    assert key == "aws-key"


def test_bedrock_get_sidecar_cmd_args():
    p = _bedrock()
    args = p.get_sidecar_cmd_args()
    assert isinstance(args, list)
    # Bedrock doesn't need extra cmd args
