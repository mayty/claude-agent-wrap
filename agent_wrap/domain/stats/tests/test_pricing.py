# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for pricing, cost, and usage classification."""

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.pricing.models import Bucket, TokenUsage
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.fold import fold_cells
from agent_wrap.infrastructure.logs.models import UsageCell
from agent_wrap.lib.log_records import usage_source

if TYPE_CHECKING:
    from collections.abc import Callable

    import pytest_mock

    from agent_wrap.conftest import FakeProvider


_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}


@pytest.fixture
def fake_provider(make_fake_provider: Callable[..., FakeProvider]) -> FakeProvider:
    """Return a FakeProvider with the default Opus 4.8 rates."""
    return make_fake_provider(flat={"claude-opus-4-8": _RATES})


@pytest.fixture
def pricing_service(
    mocker: pytest_mock.MockerFixture, fake_provider: FakeProvider, display_mock: Mock
) -> PricingService:
    """Return a PricingService backed by a mocked ProviderService (priced)."""
    mockps = mocker.Mock(spec=ProviderService)
    mockps.get_provider.return_value = fake_provider
    return PricingService(provider_service=mockps, display_service=display_mock)


@pytest.fixture
def pricing_service_empty(
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
    make_fake_provider: Callable[..., FakeProvider],
) -> PricingService:
    """Return a PricingService backed by a mocked ProviderService (no pricing data)."""
    empty = make_fake_provider(flat={})
    mockps = mocker.Mock(spec=ProviderService)
    mockps.get_provider.return_value = empty
    return PricingService(provider_service=mockps, display_service=display_mock)


@pytest.fixture
def success_rec() -> dict[str, Any]:
    """Build a minimal success record."""
    return {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }


def _request_with_ttls(*ttls: str | None) -> dict[str, Any]:
    """Build a request whose system blocks carry one cache_control per `ttls`."""
    system = []
    for ttl in ttls:
        cc: dict[str, Any] = {"type": "ephemeral"}
        if ttl is not None:
            cc["ttl"] = ttl
        system.append({"type": "text", "text": "x", "cache_control": cc})
    return {"body": {"data": {"system": system}}}


def _flat_cache_response(cw: int = 1000) -> dict[str, Any]:
    """Build a response carrying only the flat cache_creation_input_tokens (Bedrock)."""
    return {
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 100,
            "cache_creation_input_tokens": cw,
            "cache_read_input_tokens": 0,
        }
    }


def _slo_rec(model: str = "claude-opus-4-8") -> dict[str, Any]:
    """Build a success record whose usage was recovered from the standard logging object."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": 1_700_000_000.0},
        "response": {
            "_usage_source": "standard_logging_object",
            "usage": {"prompt_tokens": 800, "completion_tokens": 200},
        },
    }


def _unrecoverable_rec(model: str = "claude-opus-4-8") -> dict[str, Any]:
    """Build a success record the callback could not recover any usage for."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": 1_700_000_000.0},
        "response": {"_usage_source": "unrecoverable", "_raw_response": "<Response [200 OK]>"},
    }


def _cell(  # noqa: PLR0913 -- one keyword per field of the cell it builds
    *,
    hour: str = "2026-07-20T06",
    requests: int = 1,
    usage_source: str = "native",
    cache_write: int = 0,
    cache_write_5m: int = 0,
    cache_write_1h: int = 0,
) -> UsageCell:
    """
    Build one usage cell, addressed by an ISO ``YYYY-MM-DDTHH`` UTC hour.

    The hour is spelled out rather than given as a bucket number because that is what
    the assertions are about — an epoch hour count is unreadable and a test that got it
    wrong would look right.
    """
    started = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=UTC)
    return UsageCell(
        hour_bucket=int(started.timestamp()) // 3600,
        project_hash="hashA",
        provider="litellm-bedrock",
        session_id=1,
        model="claude-opus-4-8",
        usage_source=usage_source,
        requests=requests,
        last_started_at_us=int(started.timestamp()) * 1_000_000,
        input_tokens=100,
        output_tokens=50,
        cache_write_tokens=cache_write,
        cache_write_5m=cache_write_5m,
        cache_write_1h=cache_write_1h,
        cache_read=0,
    )


def _bucket_from_usage(usage: TokenUsage, *, msgs: int, unrecorded: int = 0) -> Bucket:
    """Stand in for ``PricingService.bucket_from_usage`` on a mocked pricing service."""
    bucket = Bucket()
    bucket.add(usage, 0.0)
    bucket.msgs = msgs
    bucket.unrecorded = unrecorded
    return bucket


def test_date_stamped_request_resolves_to_base_tier(
    mocker: pytest_mock.MockerFixture,
    fake_provider: FakeProvider,
    display_mock: Mock,
) -> None:
    mockps = mocker.Mock(spec=ProviderService)
    mockps.get_provider.return_value = fake_provider
    pricing = PricingService(provider_service=mockps, display_service=display_mock)
    usage: TokenUsage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    cost = pricing.compute_cost(
        "bedrock", "us.anthropic.claude-opus-4-8-20260514", usage=usage, hour=0
    )
    # 1000 * 5.5/1M + 500 * 27.5/1M = 0.0055 + 0.01375 = 0.01925
    assert cost is not None
    assert cost == pytest.approx(0.01925)


def test_unknown_model_returns_none(
    mocker: pytest_mock.MockerFixture,
    fake_provider: FakeProvider,
    display_mock: Mock,
) -> None:
    mockps = mocker.Mock(spec=ProviderService)
    mockps.get_provider.return_value = fake_provider
    pricing = PricingService(provider_service=mockps, display_service=display_mock)
    usage: TokenUsage = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation": {},
    }
    assert pricing.compute_cost("bedrock", "claude-opus-4-5", usage=usage, hour=0) is None


def test_price_buckets_prices_per_hour_and_collapses() -> None:
    """Cells in different UTC hours are priced at their own hour, then collapsed."""
    pricing = Mock(spec=PricingService)
    pricing.new_bucket.side_effect = Bucket
    pricing.bucket_from_usage.side_effect = _bucket_from_usage
    pricing.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
    pricing.compute_cost.return_value = 0.001

    cells = [_cell(hour="2026-07-20T06"), _cell(hour="2026-07-20T10")]
    folded = fold_cells(cells, pricing)

    by_day = folded["hashA"].by_day
    model = "litellm-bedrock/claude-opus-4-8"
    assert set(by_day["2026-07-20"]) == {model}
    assert by_day["2026-07-20"][model].msgs == 2
    hours = {call.kwargs["hour"] for call in pricing.compute_cost.call_args_list}
    assert hours == {6, 10}
    weekdays = {call.kwargs["weekday"] for call in pricing.compute_cost.call_args_list}
    assert weekdays == {0}  # both cells fall on 2026-07-20 (a Monday)


def test_folding_a_cell_counts_all_its_requests_not_one() -> None:
    """
    A cell is pre-summed, which is the one way it differs from a record.

    ``Bucket.add`` increments ``msgs`` by exactly one, so a fold that used it would
    report a session of 40 requests as 1. The adapter merges a whole cell's worth
    instead, and this is the assertion that keeps it doing so.
    """
    pricing = Mock(spec=PricingService)
    pricing.new_bucket.side_effect = Bucket
    pricing.bucket_from_usage.side_effect = _bucket_from_usage
    pricing.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
    pricing.compute_cost.return_value = 0.0

    folded = fold_cells([_cell(requests=40)], pricing)

    model = "litellm-bedrock/claude-opus-4-8"
    assert folded["hashA"].by_day["2026-07-20"][model].msgs == 40


def test_a_cells_stored_cache_split_is_not_spent() -> None:
    """
    The whole cache-write total is charged at the 5m rate, split or no split.

    Pinned deliberately rather than as an oversight: today's arithmetic reads the split
    from ``usage.cache_creation``, which nothing in the real tree writes, so every cache
    write already falls through to 5m. Honouring the stored split here would change
    reported spend as a side effect of a storage change. See ``usage_from_cell``.
    """
    pricing = Mock(spec=PricingService)
    pricing.new_bucket.side_effect = Bucket
    pricing.bucket_from_usage.side_effect = _bucket_from_usage
    pricing.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
    pricing.compute_cost.return_value = 0.0

    cell = _cell(cache_write=3200, cache_write_5m=1200, cache_write_1h=2000)
    folded = fold_cells([cell], pricing)

    bucket = folded["hashA"].by_day["2026-07-20"]["litellm-bedrock/claude-opus-4-8"]
    assert (bucket.cw_5m, bucket.cw_1h) == (3200, 0)


def test_an_unrecoverable_cell_counts_every_request_as_unrecorded() -> None:
    """
    ``usage_source`` is a group key, so a whole cell is unrecorded or none of it is.

    That is what lets the footnote count requests without a per-record flag: the
    aggregate never mixes an unrecoverable request into a cell with recorded ones.
    """
    pricing = Mock(spec=PricingService)
    pricing.new_bucket.side_effect = Bucket
    pricing.bucket_from_usage.side_effect = _bucket_from_usage
    pricing.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
    pricing.compute_cost.return_value = 0.0

    folded = fold_cells([_cell(requests=3, usage_source="unrecoverable")], pricing)

    bucket = folded["hashA"].by_day["2026-07-20"]["litellm-bedrock/claude-opus-4-8"]
    assert bucket.unrecorded == 3


def test_usage_source_native(success_rec: dict[str, Any]) -> None:
    assert usage_source(success_rec) == "native"


def test_usage_source_standard_logging_object() -> None:
    assert usage_source(_slo_rec()) == "standard_logging_object"


def test_usage_source_unrecoverable_marker() -> None:
    assert usage_source(_unrecoverable_rec()) == "unrecoverable"


def test_usage_source_legacy_string() -> None:
    rec = {"status": "success", "model": "claude-opus-4-8", "response": "<Response [200 OK]>"}
    assert usage_source(rec) == "unrecoverable"


@pytest.fixture
def ps(mocker: pytest_mock.MockerFixture, display_mock: Mock) -> PricingService:
    return PricingService(
        provider_service=mocker.Mock(spec=ProviderService), display_service=display_mock
    )


def test_request_cache_ttl_default_is_5m(ps: PricingService) -> None:
    assert ps.request_cache_ttl(_request_with_ttls(None, None)) == "5m"


def test_request_cache_ttl_one_hour(ps: PricingService) -> None:
    assert ps.request_cache_ttl(_request_with_ttls("1h", "1h")) == "1h"


def test_request_cache_ttl_mixed(ps: PricingService) -> None:
    assert ps.request_cache_ttl(_request_with_ttls(None, "1h")) == "mixed"


def test_request_cache_ttl_none_without_markers(ps: PricingService) -> None:
    assert ps.request_cache_ttl({"body": {"data": {"system": []}}}) is None
    assert ps.request_cache_ttl(None) is None
    assert ps.request_cache_ttl({}) is None


def test_extract_usage_attributes_flat_total_to_5m(ps: PricingService) -> None:
    usage = ps.extract_usage(_flat_cache_response(1000), "5m")
    assert usage["cache_creation"] == {"ephemeral_5m_input_tokens": 1000}


def test_extract_usage_defaults_to_5m_without_request_ttl(ps: PricingService) -> None:
    usage = ps.extract_usage(_flat_cache_response(1000))
    assert usage["cache_creation"] == {}
    assert usage["cache_creation_input_tokens"] == 1000


def test_extract_usage_trusts_response_split_over_request_ttl(ps: PricingService) -> None:
    response = {
        "usage": {
            "prompt_tokens": 5000,
            "completion_tokens": 100,
            "cache_creation_input_tokens": 1000,
            "ephemeral_5m_input_tokens": 600,
            "ephemeral_1h_input_tokens": 400,
        }
    }
    usage = ps.extract_usage(response, "1h")
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 600,
        "ephemeral_1h_input_tokens": 400,
    }


def test_extract_usage_reads_nested_cache_creation_split(ps: PricingService) -> None:
    response = {
        "usage": {
            "input_tokens": 2048,
            "cache_read_input_tokens": 1800,
            "cache_creation_input_tokens": 248,
            "output_tokens": 503,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 148,
                "ephemeral_1h_input_tokens": 100,
            },
        }
    }
    usage = ps.extract_usage(response, "5m")
    assert usage["cache_creation"] == {
        "ephemeral_5m_input_tokens": 148,
        "ephemeral_1h_input_tokens": 100,
    }
