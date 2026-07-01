# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-dashscope provider."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_wrap.providers import get_provider
from agent_wrap.providers.litellm_common import LiteLLMSidecar
from agent_wrap.providers.litellm_dashscope.provider import DashscopeProvider


def _dashscope() -> DashscopeProvider:
    p = get_provider("litellm-dashscope")
    assert isinstance(p, DashscopeProvider)
    return p


def test_dashscope_master_key_prefix():
    p = _dashscope()
    assert p.master_key_prefix == "sk-ds-"


def test_dashscope_declares_litellm_sidecar():
    p = _dashscope()
    sidecars = p.sidecars()
    assert len(sidecars) == 1
    assert isinstance(sidecars[0], LiteLLMSidecar)


# --- Provider method implementations ---


def test_dashscope_get_sidecar_env():
    p = _dashscope()
    env = p.get_sidecar_env({"_secret_key": "my-dashscope-key"})
    assert env["DASHSCOPE_API_KEY"] == "my-dashscope-key"


def test_dashscope_get_agent_env():
    p = _dashscope()
    env = p.get_agent_env("sk-ds-abc123", "http://proxy:4000")
    assert env["ANTHROPIC_API_KEY"] == "sk-ds-abc123"
    assert env["ANTHROPIC_BASE_URL"] == "http://proxy:4000"


def test_dashscope_secret_description():
    p = _dashscope()
    assert p.secret_description == "DashScope (Alibaba Cloud Model Studio) API Key"
    assert p.required_secrets() == [("api_key", p.secret_description)]


def test_dashscope_get_sidecar_cmd_args():
    p = _dashscope()
    args = p.get_sidecar_cmd_args()
    assert isinstance(args, list)
    assert len(args) == 0


# --- API key approval ---


def test_api_key_approval_id():
    assert (
        DashscopeProvider._api_key_approval_id("sk-ds-12345678901234567890")
        == "12345678901234567890"
    )
    assert DashscopeProvider._api_key_approval_id("short") == "short"


def test_approve_master_key_adds_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approving a key adds its approval ID to .claude.json."""
    config_dir = tmp_path / ".claude_config"
    config_dir.mkdir()
    claude_json = config_dir / ".claude.json"
    claude_json.write_text("{}")

    p = _dashscope()
    # Patch the path resolution
    monkeypatch.setattr(p, "_claude_json_path", lambda: claude_json)

    p._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    data = json.loads(claude_json.read_text())
    assert "abcdefghijklmnopqrst" in data["customApiKeyResponses"]["approved"]


def test_approve_master_key_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Approving the same key twice doesn't duplicate the entry."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}")
    p = _dashscope()
    monkeypatch.setattr(p, "_claude_json_path", lambda: claude_json)
    p._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    p._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    data = json.loads(claude_json.read_text())
    assert data["customApiKeyResponses"]["approved"].count("abcdefghijklmnopqrst") == 1


def test_approve_master_key_skips_malformed_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Approving a key when .claude.json is malformed is a no-op."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{bad json")
    p = _dashscope()
    monkeypatch.setattr(p, "_claude_json_path", lambda: claude_json)
    p._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    assert claude_json.read_text() == "{bad json"


def test_unapprove_master_key_removes_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unapproving a key removes its approval ID."""
    claude_json = tmp_path / ".claude.json"
    data = {"customApiKeyResponses": {"approved": ["abcdefghijklmnopqrst", "other"]}}
    claude_json.write_text(json.dumps(data))
    p = _dashscope()
    monkeypatch.setattr(p, "_claude_json_path", lambda: claude_json)
    p._unapprove_master_key("sk-ds-abcdefghijklmnopqrst")
    data = json.loads(claude_json.read_text())
    assert "abcdefghijklmnopqrst" not in data["customApiKeyResponses"]["approved"]
    assert "other" in data["customApiKeyResponses"]["approved"]


def test_unapprove_nonexistent_key_noop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unapproving a key that isn't approved is a no-op."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}")
    p = _dashscope()
    monkeypatch.setattr(p, "_claude_json_path", lambda: claude_json)
    p._unapprove_master_key("sk-ds-abcdefghijklmnopqrst")  # should not raise


def test_load_claude_json_missing_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    claude_json = tmp_path / ".claude.json"
    # Don't create it
    p = _dashscope()
    monkeypatch.setattr(p, "_claude_json_path", lambda: claude_json)
    result = p._load_claude_json()
    assert result == {}
