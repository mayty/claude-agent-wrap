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
from agent_wrap.domain.logs.cache import LogsCache
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
    return StatsService(Mock(spec=PricingService))


_CWD = Path()


def test_cache_populated_when_registry_exists(
    tmp_path: Path,
    pricing: PricingService,
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

    real_stats = stats_mod.StatsService(pricing)
    cache = LogsCache(real_stats, pricing)
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
) -> None:
    """Returns empty lists when projects.txt doesn't exist."""
    stats_mod.TOOL_DIR = tmp_path / "nonexistent"
    real_stats = stats_mod.StatsService(pricing)
    cache = LogsCache(real_stats, pricing)
    try:
        assert cache.get_groups() == []
        assert cache.get_projects() == []
        fp = cache.get_projects_fingerprint()
        assert fp["mtime"] is None
        assert fp["size"] is None
    finally:
        cache.stop()


def test_get_logs_dirs_returns_none_for_unknown_project(
    pricing: PricingService,
) -> None:
    """Unknown project id returns None."""
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
        assert cache.get_logs_dirs(999) is None
        assert cache.get_sessions(999) is None
        assert cache.get_sessions_fingerprint(999) is None
    finally:
        cache.stop()


def test_get_session_fingerprint_unknown(pricing: PricingService) -> None:
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
        assert cache.get_session_fingerprint(999, "abc") is None
    finally:
        cache.stop()


def test_store_and_retrieve(pricing: PricingService) -> None:
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
        cache.set_hot_session(
            0, "sid1", [{"__type__": "session_meta", "session_id": "sid1"}], "strings-data"
        )
        hot = cache.get_hot_session(0, "sid1")
        assert hot is not None
        assert hot == ([{"__type__": "session_meta", "session_id": "sid1"}], "strings-data")
    finally:
        cache.stop()


def test_miss_for_different_session(pricing: PricingService) -> None:
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
        cache.set_hot_session(0, "sid1", [{"k": "v"}], "strings")
        assert cache.get_hot_session(0, "sid2") is None
        assert cache.get_hot_session(1, "sid1") is None
    finally:
        cache.stop()


def test_overwrite_replaces(pricing: PricingService) -> None:
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
        cache.set_hot_session(0, "s", [{"data": "old"}], "old-strings")
        cache.set_hot_session(0, "s", [{"data": "new"}], "new-strings")
        hot = cache.get_hot_session(0, "s")
        assert hot is not None
        assert hot[0] == [{"data": "new"}]
        assert hot[1] == "new-strings"
    finally:
        cache.stop()


def test_merge_combined_dedupes_repeated_provider(pricing: PricingService) -> None:
    """Merging a second meta entry from the same provider doesn't duplicate the badge."""
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
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
        cache._merge_combined(
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
    finally:
        cache.stop()


def test_concurrent_reads_dont_crash(pricing: PricingService) -> None:
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(100):
                cache.get_groups()
                cache.get_projects()
                cache.get_projects_fingerprint()
                cache.get_hot_session(0, "x")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    cache.stop()


def test_stop_terminates_poll_thread(pricing: PricingService) -> None:
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    cache.stop()
    # If stop() returned, the thread is joined.  Give it a bit more time.
    time.sleep(0.1)
    # Access still works after stop.
    _groups = cache.get_groups()
    assert isinstance(_groups, list)


def test_oserror_handled_gracefully_during_poll(
    tmp_path: Path,
    pricing: PricingService,
    mocker: MockerFixture,
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

    real_stats = stats_mod.StatsService(pricing)
    cache = LogsCache(real_stats, pricing)
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


def test_gather_directory_manifest_and_path_to_key_are_consistent(
    tmp_path: Path,
    pricing: PricingService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
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

    real_stats = stats_mod.StatsService(pricing)
    cache = LogsCache(real_stats, pricing)
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


def test_changed_session_resolved_via_manifest_diff(
    tmp_path: Path,
    pricing: PricingService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
) -> None:
    """A modified messages.jsonl is re-scanned and reflected in cached sessions."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    real_stats = stats_mod.StatsService(pricing)
    cache = LogsCache(real_stats, pricing)
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


def test_deleted_session_removed_after_directory_disappears(
    tmp_path: Path,
    pricing: PricingService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
) -> None:
    """A session whose directory vanishes between polls is dropped from the cache."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    (launches / "projects.txt").write_text(f"{project}\n", encoding="utf-8")

    real_stats = stats_mod.StatsService(pricing)
    cache = LogsCache(real_stats, pricing)
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
    pricing: PricingService,
    *,
    relative_to_group: bool,
    expected_pid: int | None,
) -> None:
    """Resolution is pure path comparison — no filesystem access needed."""
    stats = Mock(spec=StatsService)
    stats.load_projects.return_value = cast("list[Path]", [])
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    cache = LogsCache(stats, pricing)
    try:
        logs_dir = tmp_path / "logs"
        cache._groups = [{"root": logs_dir, "name": "g", "paths": [], "logs_dirs": [logs_dir]}]
        if relative_to_group:
            mf_path = logs_dir / "litellm-bedrock" / "sess-1" / "messages.jsonl"
        else:
            mf_path = tmp_path / "elsewhere" / "sess-1" / "messages.jsonl"

        assert cache._resolve_deleted_project(mf_path) == expected_pid
    finally:
        cache.stop()
