# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-bedrock provider."""

from __future__ import annotations

from agent_wrap.providers import get_provider
from agent_wrap.providers.litellm_bedrock.provider import (
    _build_pricing_table,
    _scrape_model_keys,
)
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


# --- Pricing scraper (family-agnostic) ---


def _row(name: str, keys: list[str]) -> str:
    """One pricing-table row: a model name plus its priceOf placeholders."""
    cells = "".join(
        f"<td>{{priceOf!bedrockfoundationmodels/bedrockfoundationmodels!{k}}}</td>" for k in keys
    )
    return f"<tr><td>{name}</td>{cells}</tr>"


# A minimal page with the 5-column schema (in, out, cw_5m, cw_1h, cr): an
# existing family plus the new Fable family that the old fixed opus|sonnet|haiku
# regex would have skipped.
_PAGE_HTML = (
    "<table>"
    + _row("Claude Opus 4.8", ["O_IN", "O_OUT", "O_CW5", "O_CW1", "O_CR"])
    + _row("Claude Fable 5", ["F_IN", "F_OUT", "F_CW5", "F_CW1", "F_CR"])
    + "</table>"
)


def test_scrape_model_keys_includes_fable():
    keys = _scrape_model_keys(_PAGE_HTML)
    assert "claude-opus-4-8" in keys
    assert "claude-fable-5" in keys


def test_build_pricing_table_resolves_fable_row():
    data_json = {
        "regions": {
            "US East (N. Virginia)": {
                "F_IN": {"price": "10.0"},
                "F_OUT": {"price": "50.0"},
                "F_CW5": {"price": "12.5"},
                "F_CW1": {"price": "20.0"},
                "F_CR": {"price": "1.0"},
            }
        }
    }
    table = _build_pricing_table(_PAGE_HTML, data_json, "US East (N. Virginia)")
    assert table["claude-fable-5"] == {
        "in": 10.0,
        "out": 50.0,
        "cw_5m": 12.5,
        "cw_1h": 20.0,
        "cr": 1.0,
    }
