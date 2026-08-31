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
from agent_wrap.domain.logs.io import logs_dir
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture


def _group_count(cache: LogsCache) -> int:
    """
    Count cached groups through the public accessor.

    ``get_logs_dirs`` returns None past the last group, so walking upward from 0
    yields the group count without reaching into ``_groups``.
    """
    count = 0
    while cache.get_logs_dirs(count) is not None:
        count += 1
    return count


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
    mock.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
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
def started_cache(pricing: PricingService, config_mock: ConfigService) -> Generator[LogsCache]:
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
        assert _group_count(cache) == 1

        projects = cache.get_projects()
        # Project has no sessions yet, so it shouldn't appear in projects list.
        # But it should be discoverable as a group.
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
        assert _group_count(cache) == 0
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
                started_cache.get_logs_dirs(0)
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
    assert isinstance(started_cache.get_projects(), list)


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

        def _failing_iterdir(self_path: Path) -> Generator[Path]:
            if self_path == logs_target:
                raise OSError(_simulated_msg)
            yield from original_iterdir(self_path)

        mocker.patch("pathlib.Path.iterdir", _failing_iterdir)

        # Let the poll thread run at least once.
        time.sleep(2.5)

        # The poll thread should still be alive and cache accessible.
        assert isinstance(cache.get_logs_dirs(0), list)
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

        expected: dict[Path, tuple[int, str]] = {}
        for pid in range(_group_count(cache)):
            for logs_dir in cache.get_logs_dirs(pid) or []:
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


def test_added_project_merged_incrementally(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """Adding a new project path does not disturb existing cached sessions."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a])
    cache._projects_txt_path = launches / "projects.txt"
    (launches / "projects.txt").write_text(f"{proj_a}\n", encoding="utf-8")
    cache.start()

    sessions_a = cache.get_sessions(0)
    assert sessions_a is not None
    assert len(sessions_a) == 1
    assert sessions_a[0]["session_id"] == "sess-a"
    fp_before = cache.get_sessions_fingerprint(0)

    # Add project B and merge incrementally.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b])
    cache._merge_added_paths({str(proj_b)})

    # Project A sessions unchanged.
    sessions_a = cache.get_sessions(0)
    assert sessions_a is not None
    assert sessions_a[0]["session_id"] == "sess-a"
    assert cache.get_sessions_fingerprint(0) == fp_before

    # Project B sessions present.  "proj-b" > "proj-a", so pid 1.
    sessions_b = cache.get_sessions(1)
    assert sessions_b is not None
    assert sessions_b[0]["session_id"] == "sess-b"

    cache.stop()


def test_added_path_merges_into_existing_group(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """Two raw paths sharing a .agent_stats_leaf marker merge into one group."""
    stats_mod.TOOL_DIR = tmp_path

    parent = tmp_path / "group"
    parent.mkdir()
    (parent / ".agent_stats_leaf").write_text("my-group", encoding="utf-8")

    proj_a = parent / "sub-a"
    proj_b = parent / "sub-b"
    write_session(proj_a, "litellm-bedrock", "shared-sess", [valid_record])
    # Same session id under proj_b, different record.
    second = dict(valid_record)
    second["timing"] = {"start": 2.0, "completionStart": None, "end": 2.0}
    write_session(proj_b, "litellm-bedrock", "shared-sess", [second])

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a])
    cache.start()
    assert _group_count(cache) == 1
    # logs_dirs is [logs_dir(p) for p in paths], so it tracks group membership 1:1.
    assert cache.get_logs_dirs(0) == [logs_dir(proj_a)]

    # Add proj_b — same group root, should merge.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b])
    cache._merge_added_paths({str(proj_b)})

    # Single group, now with both member paths.
    assert _group_count(cache) == 1
    assert cache.get_logs_dirs(0) == [logs_dir(proj_a), logs_dir(proj_b)]

    # Sessions merged: shared-sess count is 2.
    sessions = cache.get_sessions(0)
    assert sessions is not None
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == "shared-sess"
    assert sessions[0]["count"] == 2
    assert sessions[0]["providers"] == ["litellm-bedrock"]

    cache.stop()


def test_added_group_inserted_mid_list(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """New group root sorting between existing roots triggers pid re-indexing."""
    stats_mod.TOOL_DIR = tmp_path

    proj_a = tmp_path / "aaa-proj"
    proj_b = tmp_path / "bbb-proj"
    proj_c = tmp_path / "ccc-proj"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_c])
    cache.start()
    assert cache.get_sessions(0)[0]["session_id"] == "sess-a"  # pyrefly: ignore [unsupported-operation]
    assert cache.get_sessions(1)[0]["session_id"] == "sess-c"  # pyrefly: ignore [unsupported-operation]

    # Insert proj-b which sorts between aaa and ccc.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b, proj_c])
    cache._merge_added_paths({str(proj_b)})

    # Verify pid re-indexing: aaa at 0, bbb at 1, ccc shifted to 2.
    assert cache.get_sessions(0)[0]["session_id"] == "sess-a"  # pyrefly: ignore [unsupported-operation]
    assert cache.get_sessions(1)[0]["session_id"] == "sess-b"  # pyrefly: ignore [unsupported-operation]
    assert cache.get_sessions(2)[0]["session_id"] == "sess-c"  # pyrefly: ignore [unsupported-operation]
    assert cache.get_sessions_fingerprint(2) is not None

    cache.stop()


def test_mixed_add_and_remove_handled_incrementally(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """Both additions and removals in the same projects.txt change are handled."""
    stats_mod.TOOL_DIR = tmp_path

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_c = tmp_path / "proj-c"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b])
    cache.start()
    assert _group_count(cache) == 2

    # Replace proj_a with proj_c.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_b, proj_c])
    cache._known_project_paths = {str(proj_a), str(proj_b)}
    # Simulate _handle_projects_txt_change: prune + merge.
    cache._prune_removed_paths({str(proj_a)})
    cache._merge_added_paths({str(proj_c)})

    # Only proj-b and proj-c remain.
    assert _group_count(cache) == 2
    sessions_0 = cache.get_sessions(0)
    assert sessions_0 is not None
    assert sessions_0[0]["session_id"] == "sess-b"
    sessions_1 = cache.get_sessions(1)
    assert sessions_1 is not None
    assert sessions_1[0]["session_id"] == "sess-c"

    cache.stop()


def test_merge_added_paths_no_full_rebuild(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """_rebuild_all is not called when a path is added via _poll_once."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    (launches / "projects.txt").write_text(f"{proj_a}\n", encoding="utf-8")

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a])
    cache._projects_txt_path = launches / "projects.txt"
    cache.start()

    rebuild_spy = mocker.spy(cache, "_rebuild_all")

    # Simulate project B being added and a poll tick detecting it.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b])
    (launches / "projects.txt").write_text(f"{proj_a}\n{proj_b}\n", encoding="utf-8")
    cache._poll_once()

    rebuild_spy.assert_not_called()

    # But the new project was picked up.
    sessions_b = cache.get_sessions(1)
    assert sessions_b is not None
    assert sessions_b[0]["session_id"] == "sess-b"

    cache.stop()


def test_merge_added_paths_preserves_hot_cache_pid(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """Hot session cache key is remapped when the project's pid shifts."""
    stats_mod.TOOL_DIR = tmp_path

    proj_a = tmp_path / "aaa-proj"
    proj_c = tmp_path / "ccc-proj"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_c])
    cache.start()

    # Set hot cache for ccc at pid 0.
    cache.set_hot_session(0, "sess-c", [{"k": "v"}], "strings")
    assert cache.get_hot_session(0, "sess-c") is not None

    # Insert proj-a which sorts before proj-c, shifting ccc to pid 1.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_c])
    cache._merge_added_paths({str(proj_a)})

    # Hot cache should have moved from pid 0 to pid 1.
    assert cache.get_hot_session(0, "sess-c") is None
    hot = cache.get_hot_session(1, "sess-c")
    assert hot is not None
    assert hot[1] == "strings"

    cache.stop()
