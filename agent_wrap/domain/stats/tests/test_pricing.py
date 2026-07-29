# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for pricing, cost, and usage classification."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.cli.stats.tree import build_project_tree, flatten_tree
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.models import Bucket, TokenUsage
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.stats.constants import ORPHANED_ARCHIVE_FILENAME
from agent_wrap.domain.stats.cost import usage_source
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest_mock

    from agent_wrap.conftest import FakeProvider
    from agent_wrap.domain.stats.models import ArchiveDoc, ArchiveLeaf


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
    assert row["name"] == "runs"
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


def _archived_leaf(  # noqa: PLR0913
    *,
    msgs: int = 1,
    in_tokens: int = 1000,
    out_tokens: int = 500,
    cw_5m: int = 0,
    cw_1h: int = 0,
    cr: int = 0,
    unrecorded: int = 0,
) -> ArchiveLeaf:
    return {
        "msgs": msgs,
        "input_tokens": in_tokens,
        "output_tokens": out_tokens,
        "cache_write_5m": cw_5m,
        "cache_write_1h": cw_1h,
        "cache_read": cr,
        "unrecorded": unrecorded,
    }


def _write_archive(tmp_path: Path, doc: ArchiveDoc) -> Path:
    """Write the usage archive where StatsService will look for it."""
    path = tmp_path / ".agent-launches" / ORPHANED_ARCHIVE_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_aggregate_archived_folds_into_totals(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    _write_archive(
        tmp_path,
        {"2026-07-20": {"14": {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}}},
    )
    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}
    totals_by_source: dict[str, dict[str, Bucket]] = {}

    result = stats_svc.aggregate_archived_orphaned(
        totals_by_model, totals_by_day_by_model, totals_by_source
    )

    assert result is not None
    assert result["sessions"] == 0
    assert result["total"].msgs == 1
    assert result["total"].in_ == 1000
    assert result["total"].cost > 0
    assert totals_by_model["litellm-bedrock/claude-opus-4-8"].msgs == 1
    assert totals_by_day_by_model["2026-07-20"]["litellm-bedrock/claude-opus-4-8"].msgs == 1
    assert totals_by_source["native"]["litellm-bedrock/claude-opus-4-8"].msgs == 1


def test_aggregate_archived_reconstructs_last_ts(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """The newest in-window hour becomes last_ts, so LAST LAUNCH stays informative."""
    leaf = {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}
    _write_archive(
        tmp_path,
        {"2026-07-20": {"09": leaf, "17": leaf}, "2026-07-19": {"23": leaf}},
    )
    result = stats_svc.aggregate_archived_orphaned({}, {})
    assert result is not None
    assert result["last_ts"] == datetime(2026, 7, 20, 17, tzinfo=timezone.utc)


def test_aggregate_archived_rebuckets_day_at_read_time(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
) -> None:
    """
    Re-bucket the same archived hour under two different DAY_START_HOURS values.

    This is the whole reason the archive stores raw UTC hours instead of days: the
    user can change AGENT_DAY_START_UTC after a cleanup ran.
    """
    _write_archive(
        tmp_path,
        {"2026-07-20": {"01": {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}}},
    )

    mocker.patch("agent_wrap.domain.stats.service.DAY_START_HOURS", 0)
    by_day_zero: dict[str, dict[str, Bucket]] = {}
    stats_svc.aggregate_archived_orphaned({}, by_day_zero)
    assert set(by_day_zero) == {"2026-07-20"}

    mocker.patch("agent_wrap.domain.stats.service.DAY_START_HOURS", 2)
    by_day_two: dict[str, dict[str, Bucket]] = {}
    stats_svc.aggregate_archived_orphaned({}, by_day_two)
    assert set(by_day_two) == {"2026-07-19"}


@pytest.mark.parametrize(
    ("from_iso", "until_iso", "expected"),
    [
        (None, None, True),
        ("2026-07-01", "2026-07-31", True),
        ("2026-07-21", None, False),
        (None, "2026-07-19", False),
        ("2026-07-20", "2026-07-20", True),
    ],
)
def test_aggregate_archived_respects_window(  # noqa: PLR0913
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    from_iso: str | None,
    until_iso: str | None,
    expected: bool,  # noqa: FBT001
) -> None:
    mocker.patch("agent_wrap.domain.stats.service.DAY_START_HOURS", 0)
    _write_archive(
        tmp_path,
        {"2026-07-20": {"12": {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}}},
    )
    result = stats_svc.aggregate_archived_orphaned({}, {}, from_iso=from_iso, until_iso=until_iso)
    assert (result is not None) is expected


def test_aggregate_archived_timestampless_only_in_all_time_view(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """The "?" cell cannot be range-checked, so any bound excludes it."""
    _write_archive(
        tmp_path,
        {"?": {"?": {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}}},
    )
    assert stats_svc.aggregate_archived_orphaned({}, {}) is not None
    assert stats_svc.aggregate_archived_orphaned({}, {}, from_iso="2026-01-01") is None


def test_aggregate_archived_none_when_absent(stats_svc: StatsService) -> None:
    assert stats_svc.aggregate_archived_orphaned({}, {}) is None


def test_aggregate_archived_none_leaves_totals_untouched(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """An out-of-window archive must not inject spend it reports no row for."""
    _write_archive(
        tmp_path,
        {"2026-07-20": {"12": {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}}},
    )
    totals_by_model: dict[str, Bucket] = {}
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = {}

    result = stats_svc.aggregate_archived_orphaned(
        totals_by_model, totals_by_day_by_model, from_iso="2027-01-01"
    )

    assert result is None
    assert totals_by_model == {}
    assert totals_by_day_by_model == {}


def test_aggregate_archived_does_not_double_price_live_buckets(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """Merging must not re-price an already-priced live bucket for the same model."""
    model = "litellm-bedrock/claude-opus-4-8"
    _write_archive(tmp_path, {"2026-07-20": {"14": {model: {"native": _archived_leaf()}}}})

    # Establish the archived-only cost, then repeat with a live bucket present.
    solo = stats_svc.aggregate_archived_orphaned({}, {})
    assert solo is not None
    archived_cost = solo["total"].cost

    live = stats_svc._pricing.new_bucket()
    live.add(
        {
            "input_tokens": 1000,
            "output_tokens": 500,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        7.5,
    )
    totals_by_model = {model: live}
    stats_svc.aggregate_archived_orphaned(totals_by_model, {})

    assert totals_by_model[model].cost == pytest.approx(7.5 + archived_cost)


def test_aggregate_archived_prices_cache_tiers_separately(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """The stored 5m/1h split survives the round trip, so tiers price distinctly."""
    model = "litellm-bedrock/claude-opus-4-8"
    _write_archive(
        tmp_path,
        {
            "2026-07-20": {
                "14": {
                    model: {"native": _archived_leaf(in_tokens=0, out_tokens=0, cw_5m=1_000_000)}
                }
            }
        },
    )
    five_min = stats_svc.aggregate_archived_orphaned({}, {})

    _write_archive(
        tmp_path,
        {
            "2026-07-20": {
                "14": {
                    model: {"native": _archived_leaf(in_tokens=0, out_tokens=0, cw_1h=1_000_000)}
                }
            }
        },
    )
    one_hour = stats_svc.aggregate_archived_orphaned({}, {})

    assert five_min is not None
    assert one_hour is not None
    assert five_min["total"].cw_5m == 1_000_000
    assert one_hour["total"].cw_1h == 1_000_000
    # 1h cache writes are the pricier tier (_RATES: cw_1h 11.0 vs cw_5m 6.875).
    assert one_hour["total"].cost > five_min["total"].cost


def test_aggregate_archived_carries_unrecorded_count(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """Unrecorded requests must reach the totals so the stats footnote counts them."""
    _write_archive(
        tmp_path,
        {
            "2026-07-20": {
                "14": {
                    "litellm-bedrock/claude-opus-4-8": {
                        "unrecoverable": _archived_leaf(
                            msgs=3, in_tokens=0, out_tokens=0, unrecorded=3
                        )
                    }
                }
            }
        },
    )
    totals_by_model: dict[str, Bucket] = {}
    result = stats_svc.aggregate_archived_orphaned(totals_by_model, {})

    assert result is not None
    assert result["total"].unrecorded == 3
    assert sum(b.unrecorded for b in totals_by_model.values()) == 3


def test_aggregate_archived_tolerates_malformed_keys(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """A hand-edited archive with a bad date key must not break stats."""
    _write_archive(
        tmp_path,
        {"not-a-date": {"99": {"litellm-bedrock/claude-opus-4-8": {"native": _archived_leaf()}}}},
    )
    result = stats_svc.aggregate_archived_orphaned({}, {})
    # Falls back to the "?" day bucket rather than raising.
    assert result is not None
    assert result["total"].msgs == 1


def test_merge_orphaned_results_combines_both(stats_svc: StatsService) -> None:
    live_total = stats_svc._pricing.new_bucket()
    live_total.add(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        1.0,
    )
    archived_total = stats_svc._pricing.new_bucket()
    archived_total.add(
        {
            "input_tokens": 20,
            "output_tokens": 7,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {},
        },
        2.0,
    )
    live_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    archived_ts = datetime(2026, 7, 20, tzinfo=timezone.utc)

    merged = stats_svc.merge_orphaned_results(
        {"sessions": 2, "last_ts": live_ts, "total": live_total},
        {"sessions": 0, "last_ts": archived_ts, "total": archived_total},
    )

    assert merged is not None
    assert merged["sessions"] == 2
    assert merged["last_ts"] == archived_ts
    assert merged["total"].msgs == 2
    assert merged["total"].in_ == 30
    assert merged["total"].cost == pytest.approx(3.0)
    # Neither input is mutated.
    assert live_total.in_ == 10
    assert archived_total.in_ == 20


def test_merge_orphaned_results_passes_through_single_side(stats_svc: StatsService) -> None:
    only = {
        "sessions": 1,
        "last_ts": datetime(2026, 7, 1, tzinfo=timezone.utc),
        "total": stats_svc._pricing.new_bucket(),
    }
    assert stats_svc.merge_orphaned_results(only, None) is only  # type: ignore[arg-type]
    assert stats_svc.merge_orphaned_results(None, only) is only  # type: ignore[arg-type]
    assert stats_svc.merge_orphaned_results(None, None) is None


def test_merge_orphaned_results_handles_missing_timestamps(stats_svc: StatsService) -> None:
    merged = stats_svc.merge_orphaned_results(
        {"sessions": 1, "last_ts": None, "total": stats_svc._pricing.new_bucket()},
        {"sessions": 0, "last_ts": None, "total": stats_svc._pricing.new_bucket()},
    )
    assert merged is not None
    assert merged["last_ts"] is None


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
