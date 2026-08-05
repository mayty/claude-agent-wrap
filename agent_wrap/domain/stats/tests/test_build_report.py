# This file has been created with the assistance of an AI tool.
"""Tests for StatsService.build_report — the one aggregation `agent stats` renders."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.constants import LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.models import AggregateResult, ProjectRow, UsageArgs
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    import pytest_mock


@pytest.fixture
def stats(mocker: pytest_mock.MockFixture) -> StatsService:
    """Return a StatsService whose scan/aggregate collaborators are stubbed out."""
    svc = StatsService(mocker.Mock(spec=PricingService), mocker.Mock(spec=ConfigService))
    mocker.patch.object(svc, "orphaned_log_dirs", autospec=True, return_value=[])
    mocker.patch.object(svc, "scan_log_dirs", autospec=True, return_value={})
    mocker.patch.object(
        svc, "aggregate_projects", autospec=True, return_value=AggregateResult([], {}, {}, {})
    )
    mocker.patch.object(svc, "aggregate_orphaned", autospec=True, return_value=None)
    mocker.patch.object(svc, "aggregate_archived_orphaned", autospec=True, return_value=None)
    mocker.patch.object(svc, "merge_orphaned_results", autospec=True, return_value=None)
    return svc


def _bucket(*, unrecorded: int = 0) -> Bucket:
    b = Bucket()
    for _ in range(max(unrecorded, 1)):
        b.add(
            {
                "input_tokens": 10,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation": {},
            },
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


def test_rows_without_sessions_are_dropped(stats: StatsService):
    """A registered project that logged nothing in the window is not a row."""
    stats.aggregate_projects.return_value = AggregateResult(  # pyrefly: ignore [missing-attribute]
        [_row("live", 2), _row("silent", 0)], {}, {}, {}
    )
    report = stats.build_report([], UsageArgs())
    assert [r["name"] for r in report.rows] == ["live"]


def test_both_orphaned_sources_are_merged(stats: StatsService):
    live, archived, merged = _orphaned(1), _orphaned(2), _orphaned(3)
    stats.aggregate_orphaned.return_value = live  # pyrefly: ignore [missing-attribute]
    stats.aggregate_archived_orphaned.return_value = archived  # pyrefly: ignore [missing-attribute]
    stats.merge_orphaned_results.return_value = merged  # pyrefly: ignore [missing-attribute]

    report = stats.build_report([], UsageArgs())
    stats.merge_orphaned_results.assert_called_once_with(live, archived)  # pyrefly: ignore [missing-attribute]
    assert report.orphaned is merged


def test_pattern_excluding_orphaned_skips_both_sources(stats: StatsService):
    """Both orphaned calls share the gate — they fold into the shared totals."""
    report = stats.build_report([], UsageArgs(pattern=re.compile("proj")))
    stats.orphaned_log_dirs.assert_not_called()  # pyrefly: ignore [missing-attribute]
    stats.aggregate_orphaned.assert_not_called()  # pyrefly: ignore [missing-attribute]
    stats.aggregate_archived_orphaned.assert_not_called()  # pyrefly: ignore [missing-attribute]
    assert report.orphaned is None


def test_pattern_matching_orphaned_label_keeps_it(stats: StatsService):
    stats.build_report([], UsageArgs(pattern=re.compile("orphan")))
    stats.aggregate_orphaned.assert_called_once()  # pyrefly: ignore [missing-attribute]


def test_pattern_filters_which_projects_are_scanned(stats: StatsService, tmp_path: Path):
    keep, drop = tmp_path / "keep-me", tmp_path / "drop-me"
    stats.build_report([keep, drop], UsageArgs(pattern=re.compile("keep")))
    scanned = stats.scan_log_dirs.call_args.args[0]  # pyrefly: ignore [missing-attribute]
    assert scanned == [keep / ".claude" / LITELLM_LOGS_DIRNAME]


def test_orphaned_detection_sees_unfiltered_projects(stats: StatsService, tmp_path: Path):
    """
    Orphan detection needs every registered path, not the filtered subset.

    A dir whose owner the pattern hid is still owned, so passing the filtered list
    would report it as orphaned.
    """
    a, b = tmp_path / "a", tmp_path / "b"
    stats.build_report([a, b], UsageArgs(pattern=re.compile("^$|a")))
    stats.orphaned_log_dirs.assert_called_once_with([a, b])  # pyrefly: ignore [missing-attribute]


def test_window_is_passed_to_every_collaborator(stats: StatsService):
    stats.merge_orphaned_results.return_value = _orphaned()  # pyrefly: ignore [missing-attribute]
    args = UsageArgs(from_iso="2026-07-01", until_iso="2026-07-20")
    stats.build_report([], args)

    for mock in (
        stats.scan_log_dirs,
        stats.aggregate_projects,
        stats.aggregate_orphaned,
        stats.aggregate_archived_orphaned,
    ):
        kwargs = mock.call_args.kwargs  # pyrefly: ignore [missing-attribute]
        assert (kwargs["from_iso"], kwargs["until_iso"]) == ("2026-07-01", "2026-07-20")


def test_refresh_flag_is_passed_to_collaborators(stats: StatsService):
    """``UsageArgs(refresh=True)`` re-fetches pricing in every scan path."""
    stats.build_report([], UsageArgs(refresh=True))
    for mock in (stats.scan_log_dirs, stats.aggregate_projects):
        assert mock.call_args.kwargs["refresh_pricing_data"] is True  # pyrefly: ignore [missing-attribute]
    for mock in (stats.aggregate_orphaned, stats.aggregate_archived_orphaned):
        assert mock.call_args.kwargs["refresh_pricing_data"] is True  # pyrefly: ignore [missing-attribute]


def test_no_refresh_flag_leaves_collaborators_alone(stats: StatsService):
    """A plain run never asks for a pricing re-fetch."""
    stats.build_report([], UsageArgs())
    for mock in (stats.scan_log_dirs, stats.aggregate_projects):
        assert mock.call_args.kwargs["refresh_pricing_data"] is False  # pyrefly: ignore [missing-attribute]
    for mock in (stats.aggregate_orphaned, stats.aggregate_archived_orphaned):
        assert mock.call_args.kwargs["refresh_pricing_data"] is False  # pyrefly: ignore [missing-attribute]


def test_unrecorded_is_summed_across_models(stats: StatsService):
    stats.aggregate_projects.return_value = AggregateResult(  # pyrefly: ignore [missing-attribute]
        [], {"a/m1": _bucket(unrecorded=2), "a/m2": _bucket(unrecorded=3)}, {}, {}
    )
    assert stats.build_report([], UsageArgs()).unrecorded == 5


def test_orphaned_dirs_are_scanned_with_the_projects(stats: StatsService, tmp_path: Path):
    """One scan covers both, so the orphaned dirs must be in the same call."""
    orphan_dir = tmp_path / "litellm-logs" / "deadbeef"
    stats.orphaned_log_dirs.return_value = [orphan_dir]  # pyrefly: ignore [missing-attribute]
    proj = tmp_path / "proj"
    stats.build_report([proj], UsageArgs())
    scanned = stats.scan_log_dirs.call_args.args[0]  # pyrefly: ignore [missing-attribute]
    assert scanned == [proj / ".claude" / LITELLM_LOGS_DIRNAME, orphan_dir]
