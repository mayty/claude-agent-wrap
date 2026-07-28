# This file has been created with the assistance of an AI tool.
"""Domain-layer tests for archiving and deleting orphaned log dirs."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.stats.constants import ORPHANED_ARCHIVE_FILENAME
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    import pytest_mock

_RATES = {"in": 5.5, "out": 27.5, "cw_5m": 6.875, "cw_1h": 11.0, "cr": 0.55}

# Pre-built OSError messages (ruff EM101 forbids literals at the raise site).
_DENIED = "permission denied"
_CROSS_DEVICE = "cross-device link"


class _FakeProvider(Provider):
    def __init__(self, flat: dict[str, Any] | None = None):
        super().__init__(
            sidecar_service=Mock(spec=SidecarService), display_service=Mock(spec=DisplayService)
        )
        self._flat = flat or {}

    def sidecars(self) -> list[Any]:
        return []

    def _get_pricing(self):
        return self._flat

    def _get_tiered_pricing(self):
        raise NotImplementedError


@pytest.fixture
def stats_svc(mocker: pytest_mock.MockFixture, display_mock: Mock) -> StatsService:
    """Return a StatsService whose pricing knows claude-opus-4-8."""
    mock_ps = mocker.Mock(spec=ProviderService)
    mock_ps.get_provider.return_value = _FakeProvider(flat={"claude-opus-4-8": _RATES})
    pricing = PricingService(provider_service=mock_ps, display_service=display_mock)
    return StatsService(pricing, config_service=mocker.Mock(spec=ConfigService))


@pytest.fixture
def archive_path(tmp_path: Path) -> Path:
    """Return the archive path the service writes to (AGENT_LAUNCHES_DIR is patched)."""
    return tmp_path / ".agent-launches" / ORPHANED_ARCHIVE_FILENAME


@pytest.fixture
def broken_staging_promotion(mocker: pytest_mock.MockFixture, archive_path: Path) -> None:
    """
    Make promoting the staging file over the real archive fail.

    Scoped to that one rename so the atomic write that *creates* the staging file
    still succeeds — the point is a durable staging file the caller cannot commit.
    """
    staging = archive_path.with_suffix(".new.json")
    real_replace = Path.replace

    def selective(self: Path, target: Any) -> Path:
        if self == staging:
            raise OSError(_CROSS_DEVICE)
        return real_replace(self, target)

    mocker.patch.object(Path, "replace", selective)


def _rec(ts: datetime, *, in_tokens: int = 1000, out_tokens: int = 500) -> dict[str, Any]:
    return {
        "status": "success",
        "model": "claude-opus-4-8",
        "timing": {"start": ts.timestamp()},
        "response": {"usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens}},
    }


def _write_log_dir(
    root: Path, hash_name: str, records: list[dict[str, Any]], *, session: str = "s1"
) -> Path:
    """Create a central ``<hash>`` log dir holding *records* and return it."""
    sdir = root / hash_name / "litellm-bedrock" / session
    sdir.mkdir(parents=True)
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return root / hash_name


def _total_msgs(doc: dict[str, Any]) -> int:
    return sum(
        leaf["msgs"]
        for by_hour in doc.values()
        for by_model in by_hour.values()
        for by_source in by_model.values()
        for leaf in by_source.values()
    )


# --- orphaned_disk_usage ---------------------------------------------------


def test_orphaned_disk_usage_sums_across_dirs(stats_svc: StatsService, tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    (a / "nested").mkdir(parents=True)
    b.mkdir()
    (a / "nested" / "f1").write_bytes(b"x" * 100)
    (a / "f2").write_bytes(b"x" * 50)
    (b / "f3").write_bytes(b"x" * 25)
    assert stats_svc.orphaned_disk_usage([a, b]) == 175


def test_orphaned_disk_usage_empty_dir_is_zero(stats_svc: StatsService, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert stats_svc.orphaned_disk_usage([empty]) == 0


def test_orphaned_disk_usage_no_dirs_is_zero(stats_svc: StatsService) -> None:
    assert stats_svc.orphaned_disk_usage([]) == 0


# --- archive_and_delete_orphaned ------------------------------------------


def test_archives_and_deletes(stats_svc: StatsService, tmp_path: Path, archive_path: Path) -> None:
    logs = _write_log_dir(
        tmp_path / "central", "hashA", [_rec(datetime(2026, 7, 20, 14, 5, tzinfo=timezone.utc))]
    )
    result = stats_svc.archive_and_delete_orphaned([logs])

    assert result.finalized is True
    assert result.removed == 1
    assert not logs.exists()
    doc = json.loads(archive_path.read_text(encoding="utf-8"))
    leaf = doc["2026-07-20"]["14"]["litellm-bedrock/claude-opus-4-8"]["native"]
    assert leaf["msgs"] == 1
    assert leaf["input_tokens"] == 1000


def test_removes_no_staging_file_on_success(
    stats_svc: StatsService, tmp_path: Path, archive_path: Path
) -> None:
    logs = _write_log_dir(
        tmp_path / "central", "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    result = stats_svc.archive_and_delete_orphaned([logs])
    assert result.staging_path == archive_path.with_suffix(".new.json")
    assert not result.staging_path.exists()


def test_staging_written_before_any_delete(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    archive_path: Path,
) -> None:
    """The merged stats must be durable while the source dir still exists."""
    logs = _write_log_dir(
        tmp_path / "central", "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    staging = archive_path.with_suffix(".new.json")
    observed: dict[str, Any] = {}

    def spy_rmtree(path: Path) -> None:
        observed["dir_still_there"] = path.exists()
        observed["staging_msgs"] = _total_msgs(json.loads(staging.read_text(encoding="utf-8")))

    mocker.patch("agent_wrap.domain.stats.service.shutil.rmtree", side_effect=spy_rmtree)
    stats_svc.archive_and_delete_orphaned([logs])

    assert observed["dir_still_there"] is True
    assert observed["staging_msgs"] == 1


def test_merges_into_existing_archive(
    stats_svc: StatsService, tmp_path: Path, archive_path: Path
) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_text(
        json.dumps(
            {
                "2026-07-20": {
                    "14": {
                        "litellm-bedrock/claude-opus-4-8": {
                            "native": {
                                "msgs": 5,
                                "input_tokens": 1,
                                "output_tokens": 0,
                                "cache_write_5m": 0,
                                "cache_write_1h": 0,
                                "cache_read": 0,
                                "unrecorded": 0,
                            }
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    logs = _write_log_dir(
        tmp_path / "central", "hashA", [_rec(datetime(2026, 7, 20, 14, 30, tzinfo=timezone.utc))]
    )
    stats_svc.archive_and_delete_orphaned([logs])

    doc = json.loads(archive_path.read_text(encoding="utf-8"))
    assert doc["2026-07-20"]["14"]["litellm-bedrock/claude-opus-4-8"]["native"]["msgs"] == 6


def test_archives_records_outside_any_stats_window(
    stats_svc: StatsService, tmp_path: Path, archive_path: Path
) -> None:
    """Cleanup scans unwindowed, so ancient records are preserved, not dropped."""
    logs = _write_log_dir(
        tmp_path / "central",
        "hashA",
        [
            _rec(datetime(2019, 1, 2, 3, 0, tzinfo=timezone.utc)),
            _rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc)),
        ],
    )
    stats_svc.archive_and_delete_orphaned([logs])
    doc = json.loads(archive_path.read_text(encoding="utf-8"))
    assert set(doc) == {"2019-01-02", "2026-07-20"}


def test_freed_bytes_matches_removed_dirs(stats_svc: StatsService, tmp_path: Path) -> None:
    central = tmp_path / "central"
    logs = _write_log_dir(
        central, "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    expected = stats_svc.orphaned_disk_usage([logs])
    result = stats_svc.archive_and_delete_orphaned([logs])
    assert result.freed_bytes == expected
    assert expected > 0


def test_failed_rmtree_skips_dir_and_continues(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
    archive_path: Path,
) -> None:
    """A dir that cannot be deleted must not be archived — else it double-counts."""
    central = tmp_path / "central"
    bad = _write_log_dir(
        central, "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    good = _write_log_dir(
        central, "hashB", [_rec(datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc))]
    )
    real_rmtree = shutil.rmtree

    def selective(path: Path) -> None:
        if path == bad:
            raise OSError(_DENIED)
        real_rmtree(path)

    mocker.patch("agent_wrap.domain.stats.service.shutil.rmtree", side_effect=selective)
    result = stats_svc.archive_and_delete_orphaned([bad, good])

    assert result.finalized is True
    assert result.removed == 1
    assert bad.exists()
    assert not good.exists()
    doc = json.loads(archive_path.read_text(encoding="utf-8"))
    assert set(doc) == {"2026-07-21"}


def test_failed_rmtree_excluded_from_freed_bytes(
    stats_svc: StatsService,
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    central = tmp_path / "central"
    bad = _write_log_dir(
        central, "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    good = _write_log_dir(
        central, "hashB", [_rec(datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc))]
    )
    good_size = stats_svc.orphaned_disk_usage([good])
    real_rmtree = shutil.rmtree

    def selective(path: Path) -> None:
        if path == bad:
            raise OSError(_DENIED)
        real_rmtree(path)

    mocker.patch("agent_wrap.domain.stats.service.shutil.rmtree", side_effect=selective)
    result = stats_svc.archive_and_delete_orphaned([bad, good])
    assert result.freed_bytes == good_size


@pytest.mark.usefixtures("broken_staging_promotion")
def test_failed_promotion_reports_unfinalized(
    stats_svc: StatsService,
    tmp_path: Path,
    archive_path: Path,
) -> None:
    logs = _write_log_dir(
        tmp_path / "central", "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    result = stats_svc.archive_and_delete_orphaned([logs])

    assert result.finalized is False
    assert result.archive_path == archive_path
    assert result.staging_path == archive_path.with_suffix(".new.json")
    # The staging file survives so the user can move it into place by hand.
    assert result.staging_path.is_file()


@pytest.mark.usefixtures("broken_staging_promotion")
def test_failed_promotion_stops_remaining_dirs(
    stats_svc: StatsService,
    tmp_path: Path,
) -> None:
    """Stopping keeps the second dir intact rather than deleting it unarchived."""
    central = tmp_path / "central"
    first = _write_log_dir(
        central, "hashA", [_rec(datetime(2026, 7, 20, 14, 0, tzinfo=timezone.utc))]
    )
    second = _write_log_dir(
        central, "hashB", [_rec(datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc))]
    )
    result = stats_svc.archive_and_delete_orphaned([first, second])

    assert result.finalized is False
    assert result.removed == 0
    assert not first.exists()
    assert second.exists()


def test_no_dirs_finalizes_without_writing(stats_svc: StatsService, archive_path: Path) -> None:
    result = stats_svc.archive_and_delete_orphaned([])
    assert result.finalized is True
    assert result.removed == 0
    assert result.freed_bytes == 0
    assert not archive_path.exists()


def test_empty_log_dir_is_deleted_with_empty_archive(
    stats_svc: StatsService, tmp_path: Path, archive_path: Path
) -> None:
    logs = tmp_path / "central" / "hashA"
    logs.mkdir(parents=True)
    result = stats_svc.archive_and_delete_orphaned([logs])
    assert result.removed == 1
    assert not logs.exists()
    assert json.loads(archive_path.read_text(encoding="utf-8")) == {}
