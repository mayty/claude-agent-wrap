# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-dashscope provider."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import pytest_mock

from agent_wrap.domain.providers.key_approval import _api_key_approval_id
from agent_wrap.domain.providers.litellm_dashscope.provider import DashscopeProvider
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import (
    LiteLLMSidecar,
    SidecarService,
)


@pytest.fixture
def dashscope() -> DashscopeProvider:
    """Return a DashscopeProvider with no-op sidecar."""
    svc = Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = Mock(spec=LiteLLMSidecar)
    ps = ProviderService(sidecar_service=svc)
    p = ps.get_provider("litellm-dashscope")
    assert isinstance(p, DashscopeProvider)
    return p


@pytest.fixture
def dashscope_spec(mocker: pytest_mock.MockFixture) -> DashscopeProvider:
    """Return a DashscopeProvider with spec-mocked sidecar for sidecar tests."""
    svc = mocker.Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = mocker.Mock(spec=LiteLLMSidecar)
    ps = ProviderService(sidecar_service=svc)
    p = ps.get_provider("litellm-dashscope")
    assert isinstance(p, DashscopeProvider)
    return p


def test_dashscope_master_key_prefix(dashscope: DashscopeProvider):
    assert dashscope.master_key_prefix == "sk-ds-"


def test_dashscope_declares_litellm_sidecar(dashscope_spec: DashscopeProvider):
    sidecars = dashscope_spec.sidecars()
    assert len(sidecars) == 1


# --- Provider method implementations ---


def test_dashscope_get_sidecar_env(dashscope: DashscopeProvider):
    env = dashscope.get_sidecar_env({"_secret_key": "my-dashscope-key"})
    assert env["DASHSCOPE_API_KEY"] == "my-dashscope-key"


def test_dashscope_get_agent_env(dashscope: DashscopeProvider):
    env = dashscope.get_agent_env("sk-ds-abc123", "http://proxy:4000")
    assert env["ANTHROPIC_API_KEY"] == "sk-ds-abc123"
    assert env["ANTHROPIC_BASE_URL"] == "http://proxy:4000"


def test_dashscope_secret_description(dashscope: DashscopeProvider):
    assert dashscope.secret_description == "DashScope (Alibaba Cloud Model Studio) API Key"
    assert dashscope.required_secrets() == [("api_key", dashscope.secret_description)]


def test_dashscope_get_sidecar_cmd_args(dashscope: DashscopeProvider):
    args = dashscope.get_sidecar_cmd_args()
    assert isinstance(args, list)
    assert len(args) == 0


# --- API key approval ---


def test_api_key_approval_id():
    assert _api_key_approval_id("sk-ds-12345678901234567890") == "12345678901234567890"
    assert _api_key_approval_id("short") == "short"


def test_approve_master_key_adds_entry(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, dashscope: DashscopeProvider
) -> None:
    """Approving a key adds its approval ID to .claude.json."""
    config_dir = tmp_path / ".claude_config"
    config_dir.mkdir()
    claude_json = config_dir / ".claude.json"
    claude_json.write_text("{}")

    # Patch the path resolution
    mocker.patch(
        "agent_wrap.domain.providers.key_approval._claude_json_path",
        return_value=claude_json,
    )

    dashscope._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    data = json.loads(claude_json.read_text())
    assert "abcdefghijklmnopqrst" in data["customApiKeyResponses"]["approved"]


def test_approve_master_key_idempotent(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, dashscope: DashscopeProvider
) -> None:
    """Approving the same key twice doesn't duplicate the entry."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}")
    mocker.patch(
        "agent_wrap.domain.providers.key_approval._claude_json_path",
        return_value=claude_json,
    )
    dashscope._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    dashscope._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    data = json.loads(claude_json.read_text())
    assert data["customApiKeyResponses"]["approved"].count("abcdefghijklmnopqrst") == 1


def test_approve_master_key_skips_malformed_json(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, dashscope: DashscopeProvider
) -> None:
    """Approving a key when .claude.json is malformed is a no-op."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{bad json")
    mocker.patch(
        "agent_wrap.domain.providers.key_approval._claude_json_path",
        return_value=claude_json,
    )
    dashscope._approve_master_key("sk-ds-abcdefghijklmnopqrst")
    assert claude_json.read_text() == "{bad json"


def test_unapprove_master_key_removes_entry(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, dashscope: DashscopeProvider
) -> None:
    """Unapproving a key removes its approval ID."""
    claude_json = tmp_path / ".claude.json"
    data = {"customApiKeyResponses": {"approved": ["abcdefghijklmnopqrst", "other"]}}
    claude_json.write_text(json.dumps(data))
    mocker.patch(
        "agent_wrap.domain.providers.key_approval._claude_json_path",
        return_value=claude_json,
    )
    dashscope._unapprove_master_key("sk-ds-abcdefghijklmnopqrst")
    data = json.loads(claude_json.read_text())
    assert "abcdefghijklmnopqrst" not in data["customApiKeyResponses"]["approved"]
    assert "other" in data["customApiKeyResponses"]["approved"]


def test_unapprove_nonexistent_key_noop(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, dashscope: DashscopeProvider
) -> None:
    """Unapproving a key that isn't approved is a no-op."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text("{}")
    mocker.patch(
        "agent_wrap.domain.providers.key_approval._claude_json_path",
        return_value=claude_json,
    )
    dashscope._unapprove_master_key("sk-ds-abcdefghijklmnopqrst")  # should not raise


def test_load_claude_json_missing_file(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, dashscope: DashscopeProvider
) -> None:
    claude_json = tmp_path / ".claude.json"
    # Don't create it
    mocker.patch(
        "agent_wrap.domain.providers.key_approval._claude_json_path",
        return_value=claude_json,
    )
    result = dashscope._load_claude_json()
    assert result == {}
