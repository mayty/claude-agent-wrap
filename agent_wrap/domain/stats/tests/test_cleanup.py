# This file has been created with the assistance of an AI tool.
"""
Domain-layer tests for deleting orphaned log dirs and forgetting their requests.

Two deletes have to agree: the directory on disk and the session rows in the index. The
order matters and so does what happens when half of it fails, so most of what is here
is about the halves rather than about the happy path.
"""

import shutil
from typing import TYPE_CHECKING, Any

import pytest

from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import Mock

    import pytest_mock

    from agent_wrap.conftest import FakeProvider
    from agent_wrap.domain.stats.service import StatsService

_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}

# Pre-built OSError message (ruff EM101 forbids literals at the raise site).
_DENIED = "permission denied"


@pytest.fixture
def stats_svc(
    mocker: pytest_mock.MockFixture,
    display_mock: Mock,
    make_fake_provider: Callable[..., FakeProvider],
    make_stats_service: Callable[[PricingService], StatsService],
) -> StatsService:
    """Return a StatsService over the test's own logs database, with real pricing."""
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value = make_fake_provider(flat={"claude-opus-4-8": _RATES})
    pricing = PricingService(provider_service=mock_ps, display_service=display_mock)
    return make_stats_service(pricing)


def _rec() -> dict[str, Any]:
    return {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": 1_800_000_000.0},
        "response": {"usage": {"prompt_tokens": 1000, "completion_tokens": 500}},
    }


def _indexed_requests(stats_svc: StatsService) -> int:
    """Count the requests the index still holds, over the whole of time."""
    cache = stats_svc.usage_cache(from_iso=None, until_iso=None)
    return sum(
        bucket.msgs
        for usage in cache.values()
        for by_model in usage.by_day.values()
        for bucket in by_model.values()
    )


def test_disk_usage_sums_across_dirs(stats_svc: StatsService, tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "nested").mkdir(parents=True)
    b.mkdir()
    (a / "nested" / "f1").write_bytes(b"x" * 100)
    (a / "f2").write_bytes(b"x" * 50)
    (b / "f3").write_bytes(b"x" * 25)
    assert stats_svc.orphaned_disk_usage([a, b]) == 175


def test_disk_usage_of_an_empty_dir_is_zero(stats_svc: StatsService, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert stats_svc.orphaned_disk_usage([empty]) == 0


def test_disk_usage_of_no_dirs_is_zero(stats_svc: StatsService) -> None:
    assert stats_svc.orphaned_disk_usage([]) == 0


def test_deleting_a_dir_also_forgets_its_requests(
    stats_svc: StatsService,
    tmp_path: Path,
    write_session: Callable[..., Path],
    index_logs: Callable[[], None],
) -> None:
    """
    Both halves, in one call: the spend goes with the logs.

    This is the behaviour change the archive used to prevent — cleaned-up spend stayed
    visible under ``<orphaned>`` forever, sourced from a JSON side-copy of it. Now the
    index forgets a project along with its directory.
    """
    project = tmp_path / "gone"
    project.mkdir()
    logs = write_session(project, "s1", [_rec()]).parent.parent
    index_logs()
    assert _indexed_requests(stats_svc) == 1

    result = stats_svc.delete_orphaned_logs([logs])

    assert result.removed == 1
    assert not logs.exists()
    assert _indexed_requests(stats_svc) == 0


def test_freed_bytes_matches_the_dirs_removed(
    stats_svc: StatsService,
    tmp_path: Path,
    write_session: Callable[..., Path],
) -> None:
    project = tmp_path / "gone"
    project.mkdir()
    logs = write_session(project, "s1", [_rec()]).parent.parent
    expected = stats_svc.orphaned_disk_usage([logs])

    result = stats_svc.delete_orphaned_logs([logs])

    assert result.freed_bytes == expected
    assert expected > 0


def test_a_dir_that_cannot_be_deleted_keeps_its_requests(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    write_session: Callable[..., Path],
    index_logs: Callable[[], None],
) -> None:
    """
    The rows go only for directories that are actually gone.

    Dropping them first, or unconditionally, would blank a project's spend while its
    logs sat on disk waiting for the next ingest to read them all back in — which is
    both a wrong total and a pointless re-parse.
    """
    bad_project = tmp_path / "stuck"
    good_project = tmp_path / "gone"
    for project in (bad_project, good_project):
        project.mkdir()
    bad = write_session(bad_project, "s1", [_rec()]).parent.parent
    good = write_session(good_project, "s1", [_rec()]).parent.parent
    index_logs()
    real_rmtree = shutil.rmtree

    def selective(path: Path) -> None:
        if path == bad:
            raise OSError(_DENIED)
        real_rmtree(path)

    mocker.patch("agent_wrap.domain.stats.service.shutil.rmtree", side_effect=selective)
    result = stats_svc.delete_orphaned_logs([bad, good])

    assert result.removed == 1
    assert bad.exists()
    assert not good.exists()
    # The surviving dir keeps exactly its own request, and the deleted one's is gone.
    assert _indexed_requests(stats_svc) == 1


def test_a_dir_that_cannot_be_deleted_is_excluded_from_freed_bytes(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    write_session: Callable[..., Path],
) -> None:
    bad_project = tmp_path / "stuck"
    good_project = tmp_path / "gone"
    for project in (bad_project, good_project):
        project.mkdir()
    bad = write_session(bad_project, "s1", [_rec()]).parent.parent
    good = write_session(good_project, "s1", [_rec()]).parent.parent
    good_size = stats_svc.orphaned_disk_usage([good])
    real_rmtree = shutil.rmtree

    def selective(path: Path) -> None:
        if path == bad:
            raise OSError(_DENIED)
        real_rmtree(path)

    mocker.patch("agent_wrap.domain.stats.service.shutil.rmtree", side_effect=selective)

    assert stats_svc.delete_orphaned_logs([bad, good]).freed_bytes == good_size


def test_the_directory_is_gone_before_its_rows_are(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    write_session: Callable[..., Path],
    index_logs: Callable[[], None],
) -> None:
    """
    The ordering, asserted directly rather than inferred from the outcome.

    Both orders reach the same end state when nothing fails, so only a spy sees which
    one ran — and the order is what makes the failure modes benign in the direction
    they are.
    """
    project = tmp_path / "gone"
    project.mkdir()
    logs = write_session(project, "s1", [_rec()]).parent.parent
    index_logs()
    observed: dict[str, bool] = {}

    real_delete = stats_svc._ingest.delete_projects

    def spy(hashes: list[str]) -> None:
        observed["dir_already_gone"] = not logs.exists()
        real_delete(hashes)

    mocker.patch.object(stats_svc._ingest, "delete_projects", side_effect=spy)

    stats_svc.delete_orphaned_logs([logs])

    assert observed["dir_already_gone"] is True


def test_no_dirs_deletes_nothing(stats_svc: StatsService) -> None:
    result = stats_svc.delete_orphaned_logs([])

    assert (result.removed, result.freed_bytes) == (0, 0)


def test_an_empty_log_dir_is_deleted(stats_svc: StatsService, tmp_path: Path) -> None:
    """A hash directory the sidecar created and never wrote to is still a directory."""
    logs = tmp_path / "central" / "hashA"
    logs.mkdir(parents=True)

    result = stats_svc.delete_orphaned_logs([logs])

    assert result.removed == 1
    assert not logs.exists()
