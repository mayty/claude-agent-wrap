# This file has been created with the assistance of an AI tool.
"""Tests for StatsService.build_report — the one aggregation `agent stats` renders."""

import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.models import Bucket, TokenUsage
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.models import AggregateResult, HashUsage, ProjectRow, UsageArgs
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    import pytest_mock

    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.usage import UsageRepository


@pytest.fixture
def stats(
    mocker: pytest_mock.MockFixture,
    usage_repository: UsageRepository,
    log_ingest_repository: LogIngestRepository,
) -> StatsService:
    """
    Return a StatsService whose aggregation collaborators are stubbed out.

    Stubbed on purpose: what this module tests is how ``build_report`` composes them —
    which arguments each is handed, and which are skipped — not what any of them
    computes. The composition is where the report's internal agreement lives, and it is
    invisible in a test that also has to seed a log tree.
    """
    svc = StatsService(
        pricing_service=mocker.Mock(spec=PricingService),
        config_service=mocker.Mock(spec=ConfigService),
        usage_repository=usage_repository,
        log_ingest_repository=log_ingest_repository,
    )
    mocker.patch.object(svc, "project_owners", autospec=True, return_value={})
    mocker.patch.object(svc, "usage_cache", autospec=True, return_value={})
    mocker.patch.object(
        svc, "aggregate_projects", autospec=True, return_value=AggregateResult([], {}, {})
    )
    mocker.patch.object(svc, "aggregate_orphaned", autospec=True, return_value=None)
    return svc


def _bucket(*, unrecorded: int = 0) -> Bucket:
    b = Bucket()
    for _ in range(max(unrecorded, 1)):
        b.add(
            TokenUsage(
                input_tokens=10,
                output_tokens=0,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
            0.0,
            unrecorded=bool(unrecorded),
        )
    return b


def _row(name: str, sessions: int) -> ProjectRow:
    return {
        "path": Path(f"/{name}"),
        "name": name,
        "transient": False,
        "exists": True,
        "sessions": sessions,
        "last_ts": None,
        "total": _bucket(),
        "cost": 0.0,
    }


def _orphaned(sessions: int = 1) -> dict[str, object]:
    return {"sessions": sessions, "last_ts": None, "total": _bucket()}


def test_rows_without_sessions_are_dropped(stats: StatsService) -> None:
    """A registered project that logged nothing in the window is not a row."""
    stats.aggregate_projects.return_value = AggregateResult(  # pyrefly: ignore [missing-attribute]
        [_row("live", 2), _row("silent", 0)], {}, {}
    )
    report = stats.build_report([], UsageArgs())
    assert [r["name"] for r in report.rows] == ["live"]


def test_the_orphan_row_is_whatever_the_orphan_fold_returned(stats: StatsService) -> None:
    orphaned = _orphaned(3)
    stats.aggregate_orphaned.return_value = orphaned  # pyrefly: ignore [missing-attribute]

    assert stats.build_report([], UsageArgs()).orphaned is orphaned


def test_a_pattern_excluding_orphaned_skips_the_fold(stats: StatsService) -> None:
    """
    The orphan fold shares the pattern gate, because it writes to the shared totals.

    Folding in spend whose row is suppressed would leave the by-day table describing
    requests the projects table denies exist.
    """
    report = stats.build_report([], UsageArgs(pattern=re.compile("proj")))

    stats.aggregate_orphaned.assert_not_called()  # pyrefly: ignore [missing-attribute]
    assert report.orphaned is None


def test_a_pattern_matching_the_orphaned_label_keeps_it(stats: StatsService) -> None:
    stats.build_report([], UsageArgs(pattern=re.compile("orphan")))

    stats.aggregate_orphaned.assert_called_once()  # pyrefly: ignore [missing-attribute]


def test_only_the_matching_projects_are_aggregated(stats: StatsService, tmp_path: Path) -> None:
    keep, drop = tmp_path / "keep-me", tmp_path / "drop-me"

    stats.build_report([keep, drop], UsageArgs(pattern=re.compile("keep")))

    assert stats.aggregate_projects.call_args.args[0] == [keep]  # pyrefly: ignore [missing-attribute]


def test_ownership_is_resolved_over_the_unfiltered_registry(
    stats: StatsService, tmp_path: Path
) -> None:
    """
    Ownership needs every registered path, not the filtered subset.

    A hash whose owner the pattern hid is still owned. Resolving ownership over the
    filtered list would make it orphaned instead, so a ``--pattern`` would move another
    project's spend into the ``<orphaned>`` row rather than hiding it.
    """
    a, b = tmp_path / "a", tmp_path / "b"

    stats.build_report([a, b], UsageArgs(pattern=re.compile("^$|a")))

    stats.project_owners.assert_called_once_with([a, b])  # pyrefly: ignore [missing-attribute]


def test_the_orphan_fold_is_given_the_hashes_the_registry_claims(
    stats: StatsService, tmp_path: Path
) -> None:
    """
    Orphaned is the complement of owned, computed from one set rather than re-derived.

    Handing the fold the owned hashes is what makes the two halves partition the index:
    a hash cannot be both, and none can be neither.
    """
    project = tmp_path / "proj"
    stats.project_owners.return_value = {project: "hashA"}  # pyrefly: ignore [missing-attribute]

    stats.build_report([project], UsageArgs())

    assert stats.aggregate_orphaned.call_args.args[1] == {"hashA"}  # pyrefly: ignore [missing-attribute]


def test_one_read_of_the_index_serves_every_consumer(stats: StatsService) -> None:
    """
    The cache is read once and handed to both folds, not read per fold.

    This is the whole reason the tables agree: a second read could see a request the
    first did not, and the projects table and the by-day totals would disagree by one.
    """
    cache = {"hashA": HashUsage(1, None, {})}
    stats.usage_cache.return_value = cache  # pyrefly: ignore [missing-attribute]

    stats.build_report([], UsageArgs())

    stats.usage_cache.assert_called_once()  # pyrefly: ignore [missing-attribute]
    assert stats.aggregate_projects.call_args.args[1] is cache  # pyrefly: ignore [missing-attribute]
    assert stats.aggregate_orphaned.call_args.args[0] is cache  # pyrefly: ignore [missing-attribute]


def test_the_window_reaches_the_index_read(stats: StatsService) -> None:
    """The bounds are applied once, where the cells are read; nothing else takes them."""
    stats.build_report([], UsageArgs(from_iso="2026-07-01", until_iso="2026-07-20"))

    kwargs = stats.usage_cache.call_args.kwargs  # pyrefly: ignore [missing-attribute]
    assert (kwargs["from_iso"], kwargs["until_iso"]) == ("2026-07-01", "2026-07-20")


@pytest.mark.parametrize("refresh", [True, False])
def test_the_refresh_flag_reaches_the_index_read(stats: StatsService, *, refresh: bool) -> None:
    """``--refresh`` re-fetches pricing, and pricing happens inside the fold."""
    stats.build_report([], UsageArgs(refresh=refresh))

    kwargs = stats.usage_cache.call_args.kwargs  # pyrefly: ignore [missing-attribute]
    assert kwargs["refresh_pricing_data"] is refresh


def test_unrecorded_is_summed_across_models(stats: StatsService) -> None:
    stats.aggregate_projects.return_value = AggregateResult(  # pyrefly: ignore [missing-attribute]
        [], {"a/m1": _bucket(unrecorded=2), "a/m2": _bucket(unrecorded=3)}, {}
    )
    assert stats.build_report([], UsageArgs()).unrecorded == 5


def test_a_stubbed_service_still_needs_both_repositories(
    usage_repository: UsageRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    Both are constructor dependencies, so a real service cannot be built without them.

    Asserted because it is the layering rule the whole change rests on: the service
    reaches storage only through injected repositories, never by importing one.
    """
    svc = StatsService(
        pricing_service=Mock(spec=PricingService),
        config_service=Mock(spec=ConfigService),
        usage_repository=usage_repository,
        log_ingest_repository=log_ingest_repository,
    )

    assert svc.usage_cache(from_iso=None, until_iso=None) == {}
