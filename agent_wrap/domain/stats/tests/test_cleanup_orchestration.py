# This file has been created with the assistance of an AI tool.
"""Tests for the cleanup scope/run cycle — what is surveyed, and what is mutated."""

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

    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.usage import UsageRepository

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
def stats(
    mocker: pytest_mock.MockFixture,
    config: Mock,
    usage_repository: UsageRepository,
    log_ingest_repository: LogIngestRepository,
) -> StatsService:
    """Return a StatsService whose survey and delete collaborators are stubbed."""
    svc = StatsService(
        pricing_service=Mock(spec=PricingService),
        config_service=config,
        usage_repository=usage_repository,
        log_ingest_repository=log_ingest_repository,
    )
    mocker.patch.object(svc, "orphaned_log_dirs", autospec=True, return_value=list(_ORPHANED))
    mocker.patch.object(svc, "orphaned_disk_usage", autospec=True, return_value=3_145_728)
    mocker.patch.object(
        svc,
        "delete_orphaned_logs",
        autospec=True,
        return_value=CleanupResult(removed=2, freed_bytes=2_097_152),
    )
    return svc


def test_scope_reports_both_kinds_of_leftover(stats: StatsService):
    scope = stats.cleanup_scope()
    assert scope.orphaned_dirs == _ORPHANED
    assert scope.stale_paths == _STALE
    assert scope.freed_estimate == 3_145_728
    assert scope.is_empty is False


def test_scope_is_empty_only_when_both_are(stats: StatsService, config: Mock):
    stats.orphaned_log_dirs.return_value = []  # pyrefly: ignore [missing-attribute]
    assert stats.cleanup_scope().is_empty is False  # stale entries remain

    config.stale_project_paths.return_value = cast("list[Path]", [])
    assert stats.cleanup_scope().is_empty is True


def test_orphan_detection_sees_the_whole_registry(stats: StatsService):
    """Orphan detection must see every registered project, or live dirs look orphaned."""
    stats.cleanup_scope()
    stats.orphaned_log_dirs.assert_called_once_with(_REGISTERED)  # pyrefly: ignore [missing-attribute]


def test_size_is_measured_over_the_dirs_that_will_be_deleted(stats: StatsService):
    """Measuring the same list, not re-walking, is what closes the TOCTOU gap."""
    stats.cleanup_scope()
    stats.orphaned_disk_usage.assert_called_once_with(_ORPHANED)  # pyrefly: ignore [missing-attribute]


def test_run_deletes_the_surveyed_dirs_and_prunes(stats: StatsService, config: Mock):
    scope = stats.cleanup_scope()
    outcome = stats.run_cleanup(scope)

    stats.delete_orphaned_logs.assert_called_once_with(_ORPHANED)  # pyrefly: ignore [missing-attribute]
    config.prune_stale_projects.assert_called_once_with(_STALE)
    assert outcome.removed_paths == _STALE
    assert outcome.result.removed == 2


def test_the_registry_is_pruned_even_when_a_dir_survived(stats: StatsService, config: Mock):
    """
    A failed ``rmtree`` no longer holds the registry hostage.

    It used to: the two-phase archive could leave spend committed only to a staging
    file, and pruning the registry then would have destroyed the one clue about what
    the dirs had been. With no archive there is no half-committed state to protect —
    a dir that survives is simply still orphaned next time.
    """
    stats.delete_orphaned_logs.return_value = CleanupResult(removed=1, freed_bytes=8)  # pyrefly: ignore [missing-attribute]

    outcome = stats.run_cleanup(stats.cleanup_scope())

    config.prune_stale_projects.assert_called_once_with(_STALE)
    assert outcome.removed_paths == _STALE


def test_run_acts_on_the_scope_it_is_given(stats: StatsService, config: Mock):
    """A caller may survey once and act later; the passed scope is authoritative."""
    explicit = CleanupScope(
        orphaned_dirs=[Path("/only/this")], stale_paths=[Path("/only/stale")], freed_estimate=1
    )
    stats.run_cleanup(explicit)

    stats.delete_orphaned_logs.assert_called_once_with([Path("/only/this")])  # pyrefly: ignore [missing-attribute]
    config.prune_stale_projects.assert_called_once_with([Path("/only/stale")])
