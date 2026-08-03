# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.pricing.service.PricingService."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.service import ProviderService

if TYPE_CHECKING:
    from agent_wrap.domain.pricing.models import TokenUsage


@pytest.fixture
def provider_mock() -> Mock:
    return Mock(spec=ProviderService)


@pytest.fixture
def svc(display_mock: Mock, provider_mock: Mock) -> PricingService:
    return PricingService(provider_service=provider_mock, display_service=display_mock)


def _zero_usage() -> TokenUsage:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }


def test_new_bucket_is_zero(svc: PricingService) -> None:
    b = svc.new_bucket()
    assert b.msgs == 0
    assert b.in_ == 0
    assert b.out == 0
    assert b.cw_5m == 0
    assert b.cw_1h == 0
    assert b.cr == 0
    assert b.cost == 0.0
    assert b.cost_unknown is False
    assert b.unrecorded == 0


def test_bucket_add_increments_counts(svc: PricingService) -> None:
    b = svc.new_bucket()
    usage: TokenUsage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 20,
        "cache_read_input_tokens": 10,
        "cache_creation": {"ephemeral_5m_input_tokens": 20},
    }
    b.add(usage, request_cost=0.005)
    assert b.msgs == 1
    assert b.in_ == 100
    assert b.out == 50
    assert b.cw_5m == 20
    assert b.cw_1h == 0
    assert b.cr == 10
    assert b.cost == 0.005


def test_bucket_add_unknown_cost(svc: PricingService) -> None:
    b = svc.new_bucket()
    usage: TokenUsage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    b.add(usage, request_cost=None)
    assert b.cost_unknown is True
    assert b.cost == 0.0


def test_bucket_add_unrecorded(svc: PricingService) -> None:
    b = svc.new_bucket()
    b.add(_zero_usage(), request_cost=0.0, unrecorded=True)
    assert b.unrecorded == 1


def test_bucket_from_usage_sets_aggregate_counters(svc: PricingService) -> None:
    """Pre-summed callers set msgs explicitly — add() would only ever count one."""
    usage: TokenUsage = {
        "input_tokens": 300,
        "output_tokens": 150,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 40,
        "cache_creation": {},
    }
    b = svc.bucket_from_usage(usage, msgs=7, unrecorded=2)
    assert b.msgs == 7
    assert b.unrecorded == 2
    assert b.in_ == 300
    assert b.out == 150
    assert b.cr == 40
    assert b.cost == 0.0
    assert b.cost_unknown is False


def test_bucket_from_usage_defaults_unrecorded_to_zero(svc: PricingService) -> None:
    b = svc.bucket_from_usage(_zero_usage(), msgs=3)
    assert b.unrecorded == 0


def test_bucket_from_usage_preserves_explicit_cache_tiers(svc: PricingService) -> None:
    """An explicit split must pass through untouched, not hit add()'s flat fallback."""
    usage: TokenUsage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 300,
        "cache_read_input_tokens": 0,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 100,
            "ephemeral_1h_input_tokens": 200,
        },
    }
    b = svc.bucket_from_usage(usage, msgs=1)
    assert b.cw_5m == 100
    assert b.cw_1h == 200


def test_bucket_from_usage_applies_flat_cache_fallback(svc: PricingService) -> None:
    """Without a split, the flat total still lands on the 5m tier via Bucket.add."""
    usage: TokenUsage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_creation_input_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    b = svc.bucket_from_usage(usage, msgs=1)
    assert b.cw_5m == 500
    assert b.cw_1h == 0


def test_bucket_from_usage_returns_independent_buckets(svc: PricingService) -> None:
    first = svc.bucket_from_usage(_zero_usage(), msgs=1)
    second = svc.bucket_from_usage(_zero_usage(), msgs=1)
    first.merge(second)
    assert first.msgs == 2
    assert second.msgs == 1


def test_bucket_add_falls_back_to_5m_when_no_ephemeral_split(svc: PricingService) -> None:
    """When cache_creation has no ephemeral keys, cw_5m gets the flat total."""
    b = svc.new_bucket()
    usage: TokenUsage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    b.add(usage)
    assert b.cw_5m == 30
    assert b.cw_1h == 0


def test_bucket_add_uses_ephemeral_split(svc: PricingService) -> None:
    """When cache_creation has ephemeral keys, they take precedence."""
    b = svc.new_bucket()
    usage: TokenUsage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_creation_input_tokens": 30,
        "cache_read_input_tokens": 0,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 10,
            "ephemeral_1h_input_tokens": 20,
        },
    }
    b.add(usage)
    assert b.cw_5m == 10
    assert b.cw_1h == 20


def test_bucket_merge_combines_buckets(svc: PricingService) -> None:
    a = svc.new_bucket()
    usage: TokenUsage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    a.add(usage, request_cost=1.0)

    b = svc.new_bucket()
    b.add(usage, request_cost=2.0)
    b.cost_unknown = True
    b.unrecorded = 3

    a.merge(b)
    assert a.msgs == 2
    assert a.in_ == 200
    assert a.out == 100
    assert a.cost == 3.0
    assert a.cost_unknown is True
    assert a.unrecorded == 3


def test_bucket_cw_property(svc: PricingService) -> None:
    b = svc.new_bucket()
    b.cw_5m = 10
    b.cw_1h = 20
    assert b.cw == 30


def test_normalize_canonical_claude(svc: PricingService) -> None:
    assert svc.normalize_model("claude-opus-4-7") == "claude-opus-4-7"
    assert svc.normalize_model("claude-sonnet-4-5") == "claude-sonnet-4-5"


def test_normalize_date_stamped(svc: PricingService) -> None:
    assert svc.normalize_model("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"


def test_normalize_bedrock_model_id(svc: PricingService) -> None:
    assert svc.normalize_model("anthropic.claude-opus-4-7-v1:0") == "claude-opus-4-7"
    # Date-suffixed model with Bedrock suffix: the date is retained because
    # DATE_SUFFIX_RE only strips at end-of-string, and -v1:0 follows it.
    assert (
        svc.normalize_model("us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        == "claude-sonnet-4-5-20250929"
    )


def test_normalize_display_name_t_form(svc: PricingService) -> None:
    """Claude Opus 4.7 → claude-opus-4-7."""
    assert svc.normalize_model("Claude Opus 4.7") == "claude-opus-4-7"
    assert svc.normalize_model("Claude Sonnet 4.5") == "claude-sonnet-4-5"


def test_normalize_display_name_v_form(svc: PricingService) -> None:
    """Claude 4 Opus → claude-opus-4."""
    assert svc.normalize_model("Claude 4 Opus") == "claude-opus-4"
    assert svc.normalize_model("Claude 5 Sonnet") == "claude-sonnet-5"


def test_normalize_empty_or_non_claude(svc: PricingService) -> None:
    assert svc.normalize_model("") is None
    assert svc.normalize_model("gpt-4") is None
    assert svc.normalize_model("not-a-model") is None


def test_normalize_model_with_slash(svc: PricingService) -> None:
    """Model ids with provider prefix (handled by compute_cost, not normalize)."""
    assert svc.normalize_model("bedrock/claude-opus-4-7") == "claude-opus-4-7"


def test_request_cache_ttl_no_body(svc: PricingService) -> None:
    assert svc.request_cache_ttl(None) is None
    assert svc.request_cache_ttl({}) is None


def test_request_cache_ttl_no_cache_control(svc: PricingService) -> None:
    messages: list[dict[str, object]] = []
    req = {"body": {"data": {"model": "claude-opus-4-7", "messages": messages}}}
    assert svc.request_cache_ttl(req) is None


def test_request_cache_ttl_single_5m(svc: PricingService) -> None:
    req = {"body": {"data": {"messages": [{"cache_control": {"ttl": "5m"}}]}}}
    assert svc.request_cache_ttl(req) == "5m"


def test_request_cache_ttl_single_1h(svc: PricingService) -> None:
    req = {"body": {"data": {"messages": [{"content": [{"cache_control": {"ttl": "1h"}}]}]}}}
    assert svc.request_cache_ttl(req) == "1h"


def test_request_cache_ttl_mixed(svc: PricingService) -> None:
    req = {
        "body": {
            "data": {
                "messages": [
                    {"cache_control": {"ttl": "5m"}},
                    {"cache_control": {"ttl": "1h"}},
                ]
            }
        }
    }
    assert svc.request_cache_ttl(req) == "mixed"


def test_request_cache_ttl_nested_in_content_array(svc: PricingService) -> None:
    req = {
        "body": {
            "data": {
                "system": [{"cache_control": {"ttl": "1h"}}],
                "messages": [{"content": "hello"}],
            }
        }
    }
    assert svc.request_cache_ttl(req) == "1h"


def test_request_cache_ttl_no_data_field(svc: PricingService) -> None:
    req: dict[str, Any] = {"body": {}}
    assert svc.request_cache_ttl(req) is None


def test_request_cache_ttl_body_not_dict(svc: PricingService) -> None:
    req: dict[str, Any] = {"body": "not a dict"}
    assert svc.request_cache_ttl(req) is None


def test_response_cache_split_no_usage(svc: PricingService) -> None:
    assert svc.response_cache_split({}) == {}


def test_response_cache_split_ephemeral_keys_from_usage(svc: PricingService) -> None:
    usage: dict[str, Any] = {
        "input_tokens": 100,
        "output_tokens": 50,
        "ephemeral_5m_input_tokens": 20,
        "ephemeral_1h_input_tokens": 30,
    }
    assert svc.response_cache_split(usage) == {
        "ephemeral_5m_input_tokens": 20,
        "ephemeral_1h_input_tokens": 30,
    }


def test_response_cache_split_ephemeral_keys_from_cache_creation(svc: PricingService) -> None:
    usage: dict[str, Any] = {
        "input_tokens": 100,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 10,
            "ephemeral_1h_input_tokens": 15,
        },
    }
    assert svc.response_cache_split(usage) == {
        "ephemeral_5m_input_tokens": 10,
        "ephemeral_1h_input_tokens": 15,
    }


def test_response_cache_split_top_level_keys_override(svc: PricingService) -> None:
    """Top-level usage keys overwrite cache_creation entries (checked second)."""
    usage: dict[str, Any] = {
        "ephemeral_5m_input_tokens": 999,
        "cache_creation": {
            "ephemeral_5m_input_tokens": 10,
        },
    }
    assert svc.response_cache_split(usage) == {
        "ephemeral_5m_input_tokens": 999,
    }


def test_extract_usage_none_response(svc: PricingService) -> None:
    usage = svc.extract_usage(None)
    assert usage["input_tokens"] == 0
    assert usage["output_tokens"] == 0


def test_extract_usage_basic(svc: PricingService) -> None:
    response: dict[str, Any] = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_input_tokens": 10,
        }
    }
    usage = svc.extract_usage(response)
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 50
    assert usage["cache_read_input_tokens"] == 10
    assert usage["cache_creation_input_tokens"] == 0


def test_extract_usage_prompt_completion_fallback(svc: PricingService) -> None:
    """Falls back to prompt_tokens / completion_tokens when input/output absent."""
    response: dict[str, Any] = {"usage": {"prompt_tokens": 200, "completion_tokens": 80}}
    usage = svc.extract_usage(response)
    assert usage["input_tokens"] == 200
    assert usage["output_tokens"] == 80


def test_extract_usage_cache_creation_split(svc: PricingService) -> None:
    response: dict[str, Any] = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 30,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 20,
                "ephemeral_1h_input_tokens": 10,
            },
        }
    }
    usage = svc.extract_usage(response)
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 20,
        "ephemeral_1h_input_tokens": 10,
    }
    assert usage["cache_creation_input_tokens"] == 30


def test_extract_usage_flat_cache_write_inferred_from_ttl(svc: PricingService) -> None:
    """When no ephemeral split, infer from request_ttl."""
    response: dict[str, Any] = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 30,
        }
    }
    usage = svc.extract_usage(response, request_ttl="1h")
    assert usage["cache_creation"]["ephemeral_1h_input_tokens"] == 30


def test_extract_usage_mixed_ttl_warns_once(svc: PricingService, display_mock: Mock) -> None:
    response: dict[str, Any] = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 30,
        }
    }
    svc.extract_usage(response, request_ttl="mixed")
    svc.extract_usage(response, request_ttl="mixed")
    # Should warn exactly once.
    assert display_mock.warning.call_count == 1


def test_compute_cost_normalizes_and_delegates(svc: PricingService) -> None:
    usage: TokenUsage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    fake_provider = Mock(spec=Provider)
    fake_provider.compute_cost.return_value = 0.005
    svc._provider_service.get_provider.return_value = fake_provider  # pyrefly: ignore [missing-attribute]

    cost = svc.compute_cost("litellm-bedrock", "claude-opus-4-7", usage=usage)
    assert cost == 0.005
    fake_provider.compute_cost.assert_called_once_with("claude-opus-4-7", usage)


def test_compute_cost_display_name_is_normalized(svc: PricingService) -> None:
    usage = _zero_usage()
    fake_provider = Mock(spec=Provider)
    fake_provider.compute_cost.return_value = 0.0
    svc._provider_service.get_provider.return_value = fake_provider  # pyrefly: ignore [missing-attribute]

    svc.compute_cost("litellm-bedrock", "Claude Opus 4.7", usage=usage)
    fake_provider.compute_cost.assert_called_once_with("claude-opus-4-7", usage)


def test_compute_cost_unknown_provider_returns_none(
    svc: PricingService, provider_mock: Mock
) -> None:
    provider_mock.get_provider.side_effect = ValueError("no such provider")
    assert svc.compute_cost("unknown-provider", "claude-opus-4-7", usage=_zero_usage()) is None


def test_compute_cost_slash_prefixed_model(svc: PricingService) -> None:
    """Model like 'bedrock/claude-opus-4-7' → strip prefix before normalize."""
    usage = _zero_usage()
    fake_provider = Mock(spec=Provider)
    fake_provider.compute_cost.return_value = 0.0
    svc._provider_service.get_provider.return_value = fake_provider  # pyrefly: ignore [missing-attribute]

    svc.compute_cost("litellm-bedrock", "bedrock/claude-opus-4-7", usage=usage)
    fake_provider.compute_cost.assert_called_once_with("claude-opus-4-7", usage)
