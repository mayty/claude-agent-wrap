# This file has been created with the assistance of an AI tool.
"""Tests for the cleanup scope/run cycle — what is surveyed, and what is mutated."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.models import CleanupResult, CleanupScope
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    import pytest_mock

_ORPHANED = [Path("/wrap/litellm-logs/hashA"), Path("/wrap/litellm-logs/hashB")]
_STALE = [Path("/gone/project")]
_REGISTERED = [Path("/p/one"), Path("/p/two")]


@pytest.fixture
def config() -> Mock:
    cfg = Mock(spec=ConfigService)
    cfg.read_project_paths.return_value = list(_REGISTERED)
    cfg.stale_project_paths.return_value = list(_STALE)
    cfg.prune_stale_projects.return_value = list(_STALE)
    return cfg


@pytest.fixture
def stats(mocker: pytest_mock.MockFixture, config: Mock) -> StatsService:
    """Return a StatsService whose scan and delete collaborators are stubbed."""
    svc = StatsService(Mock(spec=PricingService), config)
    mocker.patch.object(svc, "orphaned_log_dirs", autospec=True, return_value=list(_ORPHANED))
    mocker.patch.object(svc, "orphaned_disk_usage", autospec=True, return_value=3_145_728)
    mocker.patch.object(
        svc, "archive_and_delete_orphaned", autospec=True, return_value=_result(finalized=True)
    )
    return svc


def _result(*, finalized: bool) -> CleanupResult:
    return CleanupResult(
        removed=2,
        freed_bytes=2_097_152,
        archive_path=Path("/a.json"),
        staging_path=Path("/a.new.json"),
        finalized=finalized,
    )


def test_scope_reports_both_kinds_of_leftover(stats: StatsService):
    scope = stats.cleanup_scope()
    assert scope.orphaned_dirs == _ORPHANED
    assert scope.stale_paths == _STALE
    assert scope.freed_estimate == 3_145_728
    assert scope.is_empty is False


def test_scope_is_empty_only_when_both_are(stats: StatsService, config: Mock):
    stats.orphaned_log_dirs.return_value = []  # type: ignore[attr-defined]
    assert stats.cleanup_scope().is_empty is False  # stale entries remain

    config.stale_project_paths.return_value = cast("list[Path]", [])
    assert stats.cleanup_scope().is_empty is True


def test_orphan_detection_sees_the_whole_registry(stats: StatsService):
    """Orphan detection must see every registered project, or live dirs look orphaned."""
    stats.cleanup_scope()
    stats.orphaned_log_dirs.assert_called_once_with(_REGISTERED)  # type: ignore[attr-defined]


def test_size_is_measured_over_the_dirs_that_will_be_deleted(stats: StatsService):
    """Measuring the same list, not re-walking, is what closes the TOCTOU gap."""
    stats.cleanup_scope()
    stats.orphaned_disk_usage.assert_called_once_with(_ORPHANED)  # type: ignore[attr-defined]


def test_run_deletes_the_surveyed_dirs_and_prunes(stats: StatsService, config: Mock):
    scope = stats.cleanup_scope()
    outcome = stats.run_cleanup(scope)

    stats.archive_and_delete_orphaned.assert_called_once_with(_ORPHANED)  # type: ignore[attr-defined]
    config.prune_stale_projects.assert_called_once_with(_STALE)
    assert outcome.removed_paths == _STALE
    assert outcome.result.finalized is True


def test_unfinalized_archive_leaves_the_registry_alone(stats: StatsService, config: Mock):
    """A half-committed archive must not also lose the registry entries."""
    stats.archive_and_delete_orphaned.return_value = _result(finalized=False)  # type: ignore[attr-defined]

    outcome = stats.run_cleanup(stats.cleanup_scope())

    config.prune_stale_projects.assert_not_called()
    assert outcome.removed_paths == []
    assert outcome.result.finalized is False


def test_run_acts_on_the_scope_it_is_given(stats: StatsService, config: Mock):
    """A caller may survey once and act later; the passed scope is authoritative."""
    explicit = CleanupScope(
        orphaned_dirs=[Path("/only/this")], stale_paths=[Path("/only/stale")], freed_estimate=1
    )
    stats.run_cleanup(explicit)

    stats.archive_and_delete_orphaned.assert_called_once_with([Path("/only/this")])  # type: ignore[attr-defined]
    config.prune_stale_projects.assert_called_once_with([Path("/only/stale")])
