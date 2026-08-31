# This file has been created with the assistance of an AI tool.
"""Tests for the litellm-anthropic-sub provider."""

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers import litellm_anthropic_sub as provider_module
from agent_wrap.domain.providers.litellm_anthropic_sub.provider import AnthropicSubProvider
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import (
    LiteLLMSidecar,
    SidecarService,
)

if TYPE_CHECKING:
    import pytest_mock


@pytest.fixture
def anthropic_sub() -> AnthropicSubProvider:
    """Return an AnthropicSubProvider with no-op sidecar."""
    svc = Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = Mock(spec=LiteLLMSidecar)
    ps = ProviderService(sidecar_service=svc, display_service=Mock(spec=DisplayService))
    p = ps.get_provider("litellm-anthropic-sub")
    assert isinstance(p, AnthropicSubProvider)
    return p


@pytest.fixture
def anthropic_sub_spec(mocker: pytest_mock.MockFixture) -> AnthropicSubProvider:
    """Return an AnthropicSubProvider with spec-mocked sidecar for sidecar tests."""
    svc = mocker.Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = mocker.Mock(spec=LiteLLMSidecar)
    ps = ProviderService(sidecar_service=svc, display_service=Mock(spec=DisplayService))
    p = ps.get_provider("litellm-anthropic-sub")
    assert isinstance(p, AnthropicSubProvider)
    return p


def test_anthropic_sub_master_key_prefix(anthropic_sub: AnthropicSubProvider):
    assert anthropic_sub.master_key_prefix == "sk-aw-ant-"


def test_anthropic_sub_declares_litellm_sidecar(anthropic_sub_spec: AnthropicSubProvider):
    svc = anthropic_sub_spec._sidecar_service
    assert anthropic_sub_spec.sidecar() is svc.create_litellm_sidecar.return_value  # pyrefly: ignore [missing-attribute]


def test_anthropic_sub_get_sidecar_env(anthropic_sub: AnthropicSubProvider):
    assert anthropic_sub.get_sidecar_env({}) == {}


def test_anthropic_sub_get_agent_env(anthropic_sub: AnthropicSubProvider):
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert env["ANTHROPIC_BASE_URL"] == "http://proxy:4000/anthropic"


def test_anthropic_sub_base_url_targets_the_passthrough_route(
    anthropic_sub: AnthropicSubProvider,
):
    """
    Dropping the /anthropic prefix silently routes traffic to LiteLLM's translating
    /v1/messages endpoint, which strips the claude-code-20250219 beta value and the
    x-anthropic-billing-header system block. Anthropic's OAuth gate then rejects
    every request that carries no other first-party marker — in practice, all of
    Claude Code's auto-approval classifier calls — with an opaque 429.
    """
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert env["ANTHROPIC_BASE_URL"].endswith("/anthropic")
    # Claude Code appends /v1/messages itself; the result must be the passthrough route.
    assert f"{env['ANTHROPIC_BASE_URL']}/v1/messages".endswith("/anthropic/v1/messages")


def test_anthropic_sub_secret_description(anthropic_sub: AnthropicSubProvider):
    assert anthropic_sub.secret_description == ""
    assert anthropic_sub.required_secrets() == []


def test_anthropic_sub_get_agent_env_seeds_the_master_key_header(
    anthropic_sub: AnthropicSubProvider,
):
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert "x-litellm-api-key: sk-aw-ant-abc123" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()


def test_anthropic_sub_get_agent_env_pins_upstream_accept_encoding(
    anthropic_sub: AnthropicSubProvider,
):
    """
    Without this, Claude Code's "gzip, deflate, br, zstd" reaches Anthropic verbatim and a
    br/zstd reply comes back undecodable: LiteLLM ships no brotli/zstandard, so httpx
    silently falls back to identity. The agent then gets compressed bytes labelled
    application/json, and the sidecar loses the usage record to a UnicodeDecodeError
    raised inside transform_response.
    """
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert "x-pass-accept-encoding: gzip" in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()


def test_anthropic_sub_accept_encoding_override_offers_nothing_httpx_cannot_decode(
    anthropic_sub: AnthropicSubProvider,
):
    """
    The br and zstd encodings are the whole failure — httpx only decodes them when the
    optional brotli/zstandard packages are installed, and LiteLLM's image has neither.
    Widening this value back to what Claude Code asks for would reinstate the bug.
    """
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    (override,) = [
        line
        for line in env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
        if line.startswith("x-pass-accept-encoding:")
    ]
    offered = {token.strip() for token in override.partition(":")[2].split(",")}
    assert offered == {"gzip"}


def test_anthropic_sub_accept_encoding_override_uses_the_x_pass_prefix(
    anthropic_sub: AnthropicSubProvider,
):
    """
    The prefix is what makes the override win. LiteLLM's forward_headers_from_request
    merges the client's own headers first and only then assigns the de-prefixed x-pass-
    ones, so a plain "accept-encoding" entry here would be the loser of that merge — and
    Claude Code's HTTP layer owns that header name on the agent->sidecar hop anyway.
    """
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    headers = env["ANTHROPIC_CUSTOM_HEADERS"].splitlines()
    assert any(line.startswith("x-pass-accept-encoding:") for line in headers)
    assert not any(line.lower().startswith("accept-encoding:") for line in headers)


def test_anthropic_sub_custom_headers_are_newline_separated(
    anthropic_sub: AnthropicSubProvider,
):
    """
    Newline is the separator Claude Code parses ANTHROPIC_CUSTOM_HEADERS on, and the one
    the sidecar layer appends x-agent-wrap-log-prefix with (sidecars/litellm.py). Joining
    on anything else would collapse every entry here into one unparseable header.
    """
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert env["ANTHROPIC_CUSTOM_HEADERS"] == (
        "x-litellm-api-key: sk-aw-ant-abc123\nx-pass-accept-encoding: gzip"
    )


@pytest.mark.parametrize(
    "credential_var",
    ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"],
)
def test_anthropic_sub_get_agent_env_sets_no_credential_var(
    anthropic_sub: AnthropicSubProvider, credential_var: str
):
    """
    Each of these would override Claude Code's active claude.ai login, replacing the
    forwarded subscription OAuth token with a different credential and moving billing
    off the subscription.
    """
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert credential_var not in env


def test_anthropic_sub_disables_nonessential_traffic_is_false(
    anthropic_sub: AnthropicSubProvider,
):
    """
    /usage and other Anthropic-backed feature checks need feature-flag evaluation,
    which CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC would otherwise suppress.
    """
    assert anthropic_sub.disable_nonessential_traffic is False


def test_anthropic_sub_autostart_logs_viewer_is_false(
    anthropic_sub: AnthropicSubProvider,
):
    """The statusline renders rate limits here, so usage.json has no reader to serve."""
    assert anthropic_sub.autostart_logs_viewer is False


def test_anthropic_sub_get_agent_env_pins_no_models(anthropic_sub: AnthropicSubProvider):
    env = anthropic_sub.get_agent_env("sk-aw-ant-abc123", "http://proxy:4000")
    assert "ANTHROPIC_MODEL" not in env
    assert not any(key.startswith("ANTHROPIC_DEFAULT_") for key in env)


def test_anthropic_sub_master_key_prefix_is_not_an_oauth_token(
    anthropic_sub: AnthropicSubProvider,
):
    assert not anthropic_sub.master_key_prefix.startswith("sk-ant-oat")


def test_anthropic_sub_config_forwards_client_headers_and_declares_no_api_key():
    """
    The billing invariant lives in this YAML file where nothing type-checks it: if
    forward_client_headers_to_llm_api were ever removed, or an api_key line added, the
    subscription OAuth token would stop reaching Anthropic (or would be replaced).
    """
    config_path = Path(provider_module.__file__).parent / "config.yaml"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    text = "\n".join(lines)
    assert "forward_client_headers_to_llm_api: true" in text
    assert "api_key:" not in text


def test_anthropic_sub_config_declares_callbacks_but_not_success_callback():
    """
    `callbacks` is what records failures (it fills litellm.callbacks, the list
    post_call_failure_hook iterates).

    `success_callback` must stay absent. It looks like the way to register for
    successes but is not: add_litellm_success_callback only routes to
    litellm._async_success_callback when _is_async_callable() is true, and that is
    False for a CustomLogger instance (no __call__). It therefore lands in the sync
    list, which passthrough requests skip entirely — so the key is pure
    misdirection for the next person debugging missing success records. Successes are
    registered in callback.py instead.
    """
    config_path = Path(provider_module.__file__).parent / "config.yaml"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    text = "\n".join(lines)
    assert "callbacks: callback.file_logger_instance" in text
    assert "success_callback:" not in text


@pytest.mark.parametrize("model", ["claude-sonnet-4-5", "claude-opus-4-1", "some-unknown-model"])
def test_anthropic_sub_compute_cost_is_always_zero(anthropic_sub: AnthropicSubProvider, model: str):
    usage = {
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 500_000,
            "ephemeral_1h_input_tokens": 500_000,
        },
    }
    cost = anthropic_sub.compute_cost(model, usage)
    assert cost == 0.0
    assert cost is not None
