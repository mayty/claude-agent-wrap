# This file has been edited with the assistance of an AI tool.
"""Tests for the litellm-bedrock provider."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers import litellm_bedrock as provider_module
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.litellm_bedrock.constants import DEFAULT_REGION_LABEL
from agent_wrap.domain.providers.litellm_bedrock.provider import (
    _BedrockPricing,
)
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import (
    LiteLLMSidecar,
    SidecarService,
)

if TYPE_CHECKING:
    import pytest_mock


@pytest.fixture
def bedrock() -> Provider:
    """Return a Provider for litellm-bedrock with no-op sidecar."""
    svc = Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = Mock(spec=LiteLLMSidecar)
    ps = ProviderService(sidecar_service=svc, display_service=Mock(spec=DisplayService))
    p = ps.get_provider("litellm-bedrock")
    assert isinstance(p, Provider)
    return p


@pytest.fixture
def bedrock_spec(mocker: pytest_mock.MockFixture) -> Provider:
    """Return a Provider with spec-mocked sidecar for sidecar tests."""
    svc = mocker.Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = mocker.Mock(spec=LiteLLMSidecar)
    ps = ProviderService(sidecar_service=svc, display_service=Mock(spec=DisplayService))
    p = ps.get_provider("litellm-bedrock")
    assert isinstance(p, Provider)
    return p


def test_bedrock_master_key_prefix(bedrock: Provider):
    p = bedrock
    assert p.master_key_prefix == "sk-aw-"


def test_bedrock_declares_litellm_sidecar(bedrock_spec: Provider):
    p = bedrock_spec
    assert p.sidecar() is p._sidecar_service.create_litellm_sidecar.return_value  # pyrefly: ignore [missing-attribute]


def test_bedrock_get_sidecar_env(bedrock: Provider):
    p = bedrock
    env = p.get_sidecar_env({"api_key": "my-aws-key"})
    assert "AWS_BEARER_TOKEN_BEDROCK" in env
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == "my-aws-key"


def test_bedrock_get_agent_env(bedrock: Provider):
    p = bedrock
    env = p.get_agent_env("sk-aw-abc123", "http://proxy:4000/bedrock")
    assert env["AWS_BEARER_TOKEN_BEDROCK"] == "sk-aw-abc123"
    assert env["ANTHROPIC_BEDROCK_BASE_URL"] == "http://proxy:4000/bedrock/bedrock"
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"
    assert env["AWS_REGION"] == "us-east-1"


def test_bedrock_secret_description(bedrock: Provider):
    p = bedrock
    assert p.secret_description == "AWS Bedrock Bearer Token"
    assert p.required_secrets() == [("api_key", p.secret_description)]


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
    keys = _BedrockPricing.scrape_model_keys(_PAGE_HTML)
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
    table = _BedrockPricing.build_pricing_table(_PAGE_HTML, data_json, "US East (N. Virginia)")
    assert table["claude-fable-5"] == {
        "in": 10.0,
        "out": 50.0,
        "cw_5m": 12.5,
        "cw_1h": 20.0,
        "cr": 1.0,
    }


def _fresh_cache(tmp_path: Path) -> Path:
    """Write a brand-new pricing cache; return its path."""
    cache_path = tmp_path / "pricing.json"
    cache_path.write_text(
        json.dumps(
            {
                "region": DEFAULT_REGION_LABEL,
                "fetched_at": time.time(),
                "prices": {"claude-sonnet-4-5": {"in": 1.0}},
            }
        ),
        encoding="utf-8",
    )
    return cache_path


def _price_data_json() -> bytes:
    """Return the AWS metered-unit JSON needed to resolve the _PAGE_HTML rows."""
    return json.dumps(
        {
            "regions": {
                DEFAULT_REGION_LABEL: {
                    "O_IN": {"price": "3.0"},
                    "O_OUT": {"price": "15.0"},
                    "O_CW5": {"price": "3.75"},
                    "O_CW1": {"price": "4.5"},
                    "O_CR": {"price": "0.3"},
                    "F_IN": {"price": "10.0"},
                    "F_OUT": {"price": "50.0"},
                    "F_CW5": {"price": "12.5"},
                    "F_CW1": {"price": "20.0"},
                    "F_CR": {"price": "1.0"},
                }
            }
        }
    ).encode()


def test_load_prices_serves_fresh_cache_without_fetching(
    tmp_path: Path, mocker: pytest_mock.MockFixture
):
    cache_path = _fresh_cache(tmp_path)
    http_get = mocker.patch.object(_BedrockPricing, "http_get", autospec=True)

    prices = _BedrockPricing.load_prices(cache_path)

    assert prices == {"claude-sonnet-4-5": {"in": 1.0}}
    http_get.assert_not_called()


def test_load_prices_force_refetches_fresh_cache(tmp_path: Path, mocker: pytest_mock.MockFixture):
    """``refresh_pricing_data=True`` bypasses even a brand-new cache and re-fetches."""
    cache_path = _fresh_cache(tmp_path)
    http_get = mocker.patch.object(_BedrockPricing, "http_get", autospec=True)
    http_get.side_effect = [_PAGE_HTML.encode(), _price_data_json()]

    prices = _BedrockPricing.load_prices(cache_path, refresh_pricing_data=True)

    # Freshly built from the mocked page, not the cached placeholder row.
    assert prices["claude-opus-4-8"]["in"] == 3.0
    assert "claude-sonnet-4-5" not in prices
    assert http_get.call_count == 2


def test_bedrock_config_routes_every_model_through_the_bedrock_wildcard():
    """
    The `bedrock/*` wildcard is what lets `/bedrock/model/<id>/invoke*` resolve for any
    model ID — including inference profiles like `us.anthropic.claude-opus-4-7` — without
    enumerating each one. Narrowing it to named models would 404 every unlisted profile.
    """
    config_path = Path(provider_module.__file__).parent / "config.yaml"
    lines = [
        line
        for line in config_path.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    ]
    text = "\n".join(lines)
    assert 'model: "bedrock/*"' in text
