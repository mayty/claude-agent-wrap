# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for pricing, cost, and usage classification."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.cli.stats.tree import build_project_tree, flatten_tree
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.pricing.models import Bucket, TokenUsage
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.stats.cost import usage_source
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class FakeProvider(Provider):
    def __init__(self, flat: dict[str, Any] | None = None, tiered: dict[str, Any] | None = None):
        super().__init__(
            sidecar_service=Mock(spec=SidecarService), display_service=Mock(spec=DisplayService)
        )
        self._flat = flat or {}
        self._tiered = tiered

    def sidecars(self) -> list[Any]:
        return []

    def _get_pricing(self):
        return self._flat

    def _get_tiered_pricing(self):
        if self._tiered is None:
            raise NotImplementedError
        return self._tiered


_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}


@pytest.fixture
def fake_provider() -> FakeProvider:
    """Return a FakeProvider with the default Opus 4.8 rates."""
    return FakeProvider(flat={"claude-opus-4-8": _RATES})


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
) -> PricingService:
    """Return a PricingService backed by a mocked ProviderService (no pricing data)."""
    empty = FakeProvider(flat={})
    mockps = mocker.Mock(spec=ProviderService)
    mockps.get_provider.return_value = empty
    return PricingService(provider_service=mockps, display_service=display_mock)


@pytest.fixture
def stats_svc(pricing_service: PricingService) -> StatsService:
    """Return a StatsService with the priced pricing_service."""
    return StatsService(pricing_service, config_service=Mock(spec=ConfigService))


@pytest.fixture
def success_rec() -> dict[str, Any]:
    """Build a minimal success record."""
    return {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_700_000_000.0},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }


def _write_session_log(project: Path, session_id: str, records: list[dict[str, Any]]) -> None:
    sdir = project / ".claude" / "litellm-logs" / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _day_epoch(day: str) -> float:
    """Local-midnight epoch seconds for a YYYY-MM-DD day string."""
    return datetime.strptime(day, "%Y-%m-%d").astimezone().timestamp()


def _dated_rec(day: str, model: str = "claude-opus-4-8") -> dict[str, Any]:
    """Build a success record whose timing.start lands on the given day (local)."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": _day_epoch(day)},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }


def _write_central_log(
    tool_dir: Path,
    hash_name: str,
    session_id: str,
    records: list[dict[str, Any]],
) -> Path:
    """Write a central <hash> log dir directly (no project symlink points at it)."""
    sdir = tool_dir / "litellm-logs" / hash_name / "litellm-bedrock" / session_id
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return tool_dir / "litellm-logs" / hash_name


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
            "usage_source": "standard_logging_object",
            "usage": {"prompt_tokens": 800, "completion_tokens": 200},
        },
    }


def _unrecoverable_rec(model: str = "claude-opus-4-8") -> dict[str, Any]:
    """Build a success record the callback could not recover any usage for."""
    return {
        "status": "success",
        "model": model,
        "timing": {"start": 1_700_000_000.0},
        "response": {"usage_source": "unrecoverable", "_raw_response": "<Response [200 OK]>"},
    }


# ---------------------------------------------------------------------------
# PricingService round-trip pricing lookup
# ---------------------------------------------------------------------------


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
    cost = pricing.compute_cost("bedrock", "us.anthropic.claude-opus-4-8-20260514", usage=usage)
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
    assert pricing.compute_cost("bedrock", "claude-opus-4-5", usage=usage) is None


# ---------------------------------------------------------------------------
# aggregate_projects
# ---------------------------------------------------------------------------


def test_aggregate_projects_merges_marked_group(
    tmp_path: Path,
    success_rec: dict[str, Any],
    stats_svc: StatsService,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".agent_stats_leaf").write_text("batch-feb\n", encoding="utf-8")
    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session_log(a, "s1", [success_rec])
    _write_session_log(b, "s2", [success_rec])
    rows, _totals, _by_day, _by_source = stats_svc.aggregate_projects([a, b])
    assert len(rows) == 1
    row = rows[0]
    assert row["path"] == runs
    assert row["name"] == "batch-feb"
    assert row["transient"] is True
    assert row["sessions"] == 2
    assert row["total"].msgs == 2


def test_aggregate_projects_empty_marker_is_transient(
    tmp_path: Path,
    success_rec: dict[str, Any],
    display_mock: Mock,
    stats_svc: StatsService,
) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / ".agent_stats_leaf").write_text("", encoding="utf-8")
    a = runs / "agent-a"
    b = runs / "agent-b"
    _write_session_log(a, "s1", [success_rec])
    _write_session_log(b, "s2", [success_rec])
    rows, _totals, _by_day, _by_source = stats_svc.aggregate_projects([a, b])
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "runs"
    assert row["transient"] is True
    display = flatten_tree(build_project_tree(rows), display=display_mock)
    group = next(dr for dr in display if dr.label.rstrip().endswith("runs"))
    assert group.transient is True
    assert " *" not in group.label


def test_aggregate_projects_keeps_unmarked_separate(
    tmp_path: Path,
    success_rec: dict[str, Any],
    stats_svc: StatsService,
) -> None:
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    _write_session_log(a, "s1", [success_rec])
    _write_session_log(b, "s2", [success_rec])
    rows, _totals, _by_day, _by_source = stats_svc.aggregate_projects([a, b])
    assert {r["name"] for r in rows} == {"proj-a", "proj-b"}
    assert all(r["transient"] is False for r in rows)


# ---------------------------------------------------------------------------
# windowing
# ---------------------------------------------------------------------------


def test_aggregate_projects_windows_sessions_and_totals(
    tmp_path: Path,
    stats_svc: StatsService,
) -> None:
    proj = tmp_path / "proj"
    _write_session_log(proj, "in", [_dated_rec("2026-06-15")])
    _write_session_log(proj, "out", [_dated_rec("2026-01-01")])
    rows, _totals, by_day, _by_source = stats_svc.aggregate_projects(
        [proj],
        from_iso="2026-06-01",
        until_iso="2026-06-30",
    )
    assert len(rows) == 1
    assert rows[0]["sessions"] == 1
    assert rows[0]["total"].msgs == 1
    assert set(by_day) == {"2026-06-15"}


# ---------------------------------------------------------------------------
# collect_orphaned
# ---------------------------------------------------------------------------


def test_aggregate_orphaned_folds_into_totals(
    mocker: pytest_mock.MockerFixture,
    tmp_path: Path,
    success_rec: dict[str, Any],
    stats_svc: StatsService,
) -> None:
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    _write_central_log(tmp_path, "hashB", "s2", [success_rec, success_rec])
    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}
    orphaned = stats_svc.aggregate_orphaned(
        [],
        totals_by_model,
        totals_by_day_by_model,
    )
    assert orphaned is not None
    assert orphaned["sessions"] == 1
    assert orphaned["total"].msgs == 2
    assert sum(b.msgs for b in totals_by_model.values()) == 2


def test_aggregate_orphaned_none_when_all_reachable(
    mocker: pytest_mock.MockerFixture,
    tmp_path: Path,
    success_rec: dict[str, Any],
    stats_svc: StatsService,
) -> None:
    mocker.patch("agent_wrap.domain.stats.service.TOOL_DIR", tmp_path)
    hash_a = _write_central_log(tmp_path, "hashA", "s1", [success_rec])
    project = tmp_path / "proj"
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(hash_a, target_is_directory=True)
    orphaned = stats_svc.aggregate_orphaned([project], {}, {}, {})
    assert orphaned is None


# ---------------------------------------------------------------------------
# usage_source
# ---------------------------------------------------------------------------


def test_usage_source_native(success_rec: dict[str, Any]) -> None:
    assert usage_source(success_rec) == "native"


def test_usage_source_standard_logging_object() -> None:
    assert usage_source(_slo_rec()) == "standard_logging_object"


def test_usage_source_unrecoverable_marker() -> None:
    assert usage_source(_unrecoverable_rec()) == "unrecoverable"


def test_usage_source_legacy_string() -> None:
    rec = {"status": "success", "model": "claude-opus-4-8", "response": "<Response [200 OK]>"}
    assert usage_source(rec) == "unrecoverable"


def test_aggregate_projects_returns_per_source_totals(
    tmp_path: Path,
    success_rec: dict[str, Any],
    stats_svc: StatsService,
) -> None:
    proj = tmp_path / "proj"
    _write_session_log(proj, "s1", [success_rec, _slo_rec(), _unrecoverable_rec()])
    _rows, _totals, _by_day, by_source = stats_svc.aggregate_projects([proj])
    # by_source is {source: {model: Bucket}} — merge model buckets within each source.
    merged: dict[str, Bucket] = defaultdict(Bucket)
    for source, by_model in by_source.items():
        for b in by_model.values():
            merged[source].merge(b)
    assert merged["native"].msgs == 1
    assert merged["standard_logging_object"].msgs == 1
    assert merged["unrecoverable"].msgs == 1
    assert merged["unrecoverable"].unrecorded == 1


# ---------------------------------------------------------------------------
# request_cache_ttl / extract_usage
# ---------------------------------------------------------------------------


@pytest.fixture
def ps(mocker: pytest_mock.MockerFixture, display_mock: Mock) -> PricingService:
    return PricingService(
        provider_service=mocker.Mock(spec=ProviderService), display_service=display_mock
    )


# ---------------------------------------------------------------------------
# request_cache_ttl
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# extract_usage
# ---------------------------------------------------------------------------


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
