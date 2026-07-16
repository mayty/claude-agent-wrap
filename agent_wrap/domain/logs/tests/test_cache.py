# This file has been created with the assistance of an AI tool.
"""Tests for the LogsCache — in-memory cache and background FS watcher."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest

import agent_wrap.domain.stats.service as stats_mod
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture


@pytest.fixture
def valid_record() -> dict[str, Any]:
    """Return a minimal record that satisfies scan_session_meta's required fields."""
    return {
        "timing": {"start": 1.0, "completionStart": None, "end": 1.0},
        "response": {},
        "model": "m",
    }


@pytest.fixture
def write_session() -> Callable[[Path, str, str, list[dict[str, Any]]], Path]:
    """Return a factory that writes a session's messages.jsonl to disk."""

    def _write(
        project: Path, provider: str, session_id: str, records: list[dict[str, Any]]
    ) -> Path:
        sdir = project / ".claude" / "litellm-logs" / provider / session_id
        sdir.mkdir(parents=True)
        with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")
        return sdir

    return _write


@pytest.fixture
def pricing() -> PricingService:
    mock = Mock(spec=PricingService)
    mock.new_bucket.side_effect = Bucket
    mock.normalize_model.side_effect = lambda m: m
    mock.request_cache_ttl.return_value = None
    mock.extract_usage.return_value = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
    }
    mock.compute_cost.return_value = 0.001
    return mock


@pytest.fixture
def isolated_stats(tmp_path: Path) -> StatsService:
    stats_mod.TOOL_DIR = tmp_path
    (tmp_path / ".agent-launches").mkdir(parents=True, exist_ok=True)
    return StatsService(Mock(spec=PricingService), Mock(spec=ConfigService))


@pytest.fixture
def real_stats(pricing: PricingService) -> StatsService:
    """Return a StatsService backed by the real (mock-priced) PricingService."""
    return StatsService(pricing, config_service=Mock(spec=ConfigService))


@pytest.fixture
def config_svc() -> ConfigService:
    return ConfigService(display_service=Mock(spec=DisplayService))


@pytest.fixture
def config_mock() -> ConfigService:
    mock = Mock(spec=ConfigService)
    mock.read_project_paths.return_value = cast("list[Path]", [])
    return mock


_CWD = Path()


@pytest.fixture
def started_cache(
    pricing: PricingService, config_mock: ConfigService
) -> Generator[LogsCache, None, None]:
    stats = Mock(spec=StatsService)
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, config_mock, pricing)
    cache.start()
    try:
        yield cache
    finally:
        cache.stop()


def test_cache_populated_when_registry_exists(
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    real_stats: StatsService,
) -> None:
    """Cache populates from a project that has a litellm-logs symlink."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True, exist_ok=True)

    project = tmp_path / "testproj"
    (project / ".claude").mkdir(parents=True)
    logs_target = tmp_path / "litellm-logs" / "abc"
    logs_target.mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(logs_target, target_is_directory=True)

    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        groups = cache.get_groups()
        assert len(groups) == 1

        projects = cache.get_projects()
        # Project has no sessions yet, so it shouldn't appear in projects list.
        # But it should be discoverable via groups.
        assert isinstance(projects, list)
    finally:
        cache.stop()


def test_cache_empty_when_no_registry(
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    real_stats: StatsService,
) -> None:
    """Returns empty lists when projects.txt doesn't exist."""
    stats_mod.TOOL_DIR = tmp_path / "nonexistent"
    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        assert cache.get_groups() == []
        assert cache.get_projects() == []
        fp = cache.get_projects_fingerprint()
        assert fp["mtime"] is None
        assert fp["size"] is None
    finally:
        cache.stop()


def test_get_logs_dirs_returns_none_for_unknown_project(
    started_cache: LogsCache,
) -> None:
    """Unknown project id returns None."""
    assert started_cache.get_logs_dirs(999) is None
    assert started_cache.get_sessions(999) is None
    assert started_cache.get_sessions_fingerprint(999) is None


def test_get_session_fingerprint_unknown(
    started_cache: LogsCache,
) -> None:
    assert started_cache.get_session_fingerprint(999, "abc") is None


def test_store_and_retrieve(started_cache: LogsCache) -> None:
    started_cache.set_hot_session(
        0, "sid1", [{"__type__": "session_meta", "session_id": "sid1"}], "strings-data"
    )
    hot = started_cache.get_hot_session(0, "sid1")
    assert hot is not None
    assert hot == ([{"__type__": "session_meta", "session_id": "sid1"}], "strings-data")


def test_miss_for_different_session(started_cache: LogsCache) -> None:
    started_cache.set_hot_session(0, "sid1", [{"k": "v"}], "strings")
    assert started_cache.get_hot_session(0, "sid2") is None
    assert started_cache.get_hot_session(1, "sid1") is None


def test_overwrite_replaces(started_cache: LogsCache) -> None:
    started_cache.set_hot_session(0, "s", [{"data": "old"}], "old-strings")
    started_cache.set_hot_session(0, "s", [{"data": "new"}], "new-strings")
    hot = started_cache.get_hot_session(0, "s")
    assert hot is not None
    assert hot[0] == [{"data": "new"}]
    assert hot[1] == "new-strings"


def test_merge_combined_dedupes_repeated_provider(
    started_cache: LogsCache,
) -> None:
    """Merging a second meta entry from the same provider doesn't duplicate the badge."""
    existing = {
        "session_id": "s1",
        "alias": None,
        "title": None,
        "count": 1,
        "first_ts": 1.0,
        "last_ts": 1.0,
        "models": ["a"],
        "providers": ["litellm-bedrock"],
    }
    started_cache._merge_combined(
        existing,
        {
            "provider": "litellm-bedrock",
            "count": 1,
            "first_ts": 5.0,
            "last_ts": 5.0,
            "models": ["b"],
            "alias": None,
            "title": None,
        },
    )
    assert existing["providers"] == ["litellm-bedrock"]
    assert existing["count"] == 2


def test_concurrent_reads_dont_crash(started_cache: LogsCache) -> None:
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(100):
                started_cache.get_groups()
                started_cache.get_projects()
                started_cache.get_projects_fingerprint()
                started_cache.get_hot_session(0, "x")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


def test_stop_terminates_poll_thread(started_cache: LogsCache) -> None:
    started_cache.stop()
    # If stop() returned, the thread is joined.  Give it a bit more time.
    time.sleep(0.1)
    # Access still works after stop.
    _groups = started_cache.get_groups()
    assert isinstance(_groups, list)


def test_oserror_handled_gracefully_during_poll(
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    mocker: MockerFixture,
    real_stats: StatsService,
) -> None:
    """An OSError during stat doesn't kill the poll thread."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    (project / ".claude").mkdir(parents=True)
    logs_target = tmp_path / "litellm-logs" / "abc"
    logs_target.mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(logs_target, target_is_directory=True)
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        # Force an OSError during the poll by making iterdir raise.
        original_iterdir = Path.iterdir
        _simulated_msg = "Simulated"

        def _failing_iterdir(self_path: Path) -> Generator[Path, None, None]:
            if self_path == logs_target:
                raise OSError(_simulated_msg)
            yield from original_iterdir(self_path)

        mocker.patch("pathlib.Path.iterdir", _failing_iterdir)

        # Let the poll thread run at least once.
        time.sleep(2.5)

        # The poll thread should still be alive and cache accessible.
        groups = cache.get_groups()
        assert isinstance(groups, list)
    finally:
        cache.stop()


def test_gather_directory_manifest_and_path_to_key_are_consistent(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
) -> None:
    """path_to_key covers exactly the manifest's keys, with correct (pid, sid)."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    (launches / "projects.txt").write_text(f"{proj_a}\n{proj_b}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        manifest, path_to_key = cache._gather_directory_manifest()
        assert manifest.keys() == path_to_key.keys()

        groups = cache.get_groups()
        expected: dict[Path, tuple[int, str]] = {}
        for pid, group in enumerate(groups):
            for logs_dir in group["logs_dirs"]:
                for provider_dir in logs_dir.iterdir():
                    for session_dir in provider_dir.iterdir():
                        expected[session_dir / "messages.jsonl"] = (pid, session_dir.name)
        assert path_to_key == expected
    finally:
        cache.stop()


def test_changed_session_resolved_via_manifest_diff(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
) -> None:
    """A modified messages.jsonl is re-scanned and reflected in cached sessions."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        sessions = cache.get_sessions(0)
        assert sessions is not None
        assert sessions[0]["count"] == 1

        with (sdir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(valid_record) + "\n")
        (sdir / "meta.json").unlink(missing_ok=True)

        cache._poll_once()

        sessions = cache.get_sessions(0)
        assert sessions is not None
        assert sessions[0]["count"] == 2
    finally:
        cache.stop()


def test_deleted_session_removed_after_directory_disappears(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
) -> None:
    """A session whose directory vanishes between polls is dropped from the cache."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        assert cache.get_session_fingerprint(0, "sess-1") is not None

        (sdir / "messages.jsonl").unlink()
        (sdir / "meta.json").unlink(missing_ok=True)
        sdir.rmdir()

        cache._poll_once()

        sessions = cache.get_sessions(0)
        assert sessions == []
        assert cache.get_session_fingerprint(0, "sess-1") is None
    finally:
        cache.stop()


@pytest.mark.parametrize(
    ("relative_to_group", "expected_pid"),
    [
        pytest.param(True, 0, id="path-inside-group"),
        pytest.param(False, None, id="path-outside-all-groups"),
    ],
)
def test_resolve_deleted_project(
    tmp_path: Path,
    started_cache: LogsCache,
    *,
    relative_to_group: bool,
    expected_pid: int | None,
) -> None:
    """Resolution is pure path comparison — no filesystem access needed."""
    logs_dir = tmp_path / "logs"
    started_cache._groups = [{"root": logs_dir, "name": "g", "paths": [], "logs_dirs": [logs_dir]}]
    if relative_to_group:
        mf_path = logs_dir / "litellm-bedrock" / "sess-1" / "messages.jsonl"
    else:
        mf_path = tmp_path / "elsewhere" / "sess-1" / "messages.jsonl"

    assert started_cache._resolve_deleted_project(mf_path) == expected_pid


# ---------------------------------------------------------------------------
# UsageTracker integration tests
# ---------------------------------------------------------------------------


def test_poll_once_writes_usage_json(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
) -> None:
    """After _poll_once, usage.json exists with totals from today's records."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        cache._poll_once()

        usage_path = tmp_path / ".claude" / "usage.json"
        assert usage_path.is_file(), f"usage.json not found at {usage_path}"
        data = json.loads(usage_path.read_text(encoding="utf-8"))
        assert "in" in data
        assert "out" in data
        assert "cache" in data
        assert "cost" in data
        assert "requests" in data
        assert "updated_at" in data
    finally:
        cache.stop()


def test_usage_tracker_responds_to_file_changes(
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
) -> None:
    """Adding records to a session file increases usage totals on the next poll."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    today_record = {
        "status": "success",
        "model": "m",
        "timing": {"start": time.time()},
        "response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
    }
    sdir = write_session(project, "litellm-bedrock", "sess-1", [today_record])
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        # First poll — seed the tracker.
        cache._poll_once()
        usage_path = tmp_path / ".claude" / "usage.json"
        initial = json.loads(usage_path.read_text(encoding="utf-8"))
        assert initial["requests"] == 1

        # Append a second record and force a rescan.
        with (sdir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(today_record) + "\n")
        (sdir / "meta.json").unlink(missing_ok=True)

        cache._poll_once()
        updated = json.loads(usage_path.read_text(encoding="utf-8"))
        assert updated["requests"] > initial["requests"]
    finally:
        cache.stop()
