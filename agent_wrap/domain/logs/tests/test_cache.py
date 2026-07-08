# This file has been created with the assistance of an AI tool.
"""Tests for the LogsCache — in-memory cache and background FS watcher."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast
from unittest.mock import Mock

import pytest

import agent_wrap.domain.stats.service as stats_mod
from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from collections.abc import Generator

    from pytest_mock import MockerFixture


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


class TestCacheConstruction:
    def test_cache_populated_when_registry_exists(
        self,
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
        self,
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


class TestAccessors:
    def test_get_logs_dirs_returns_none_for_unknown_project(
        self,
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

    def test_get_session_fingerprint_unknown(self, pricing: PricingService) -> None:
        stats = Mock(spec=StatsService)
        stats.load_projects.return_value = cast("list[Path]", [])
        stats.resolve_group.return_value = (_CWD, ".", False)
        stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
        cache = LogsCache(stats, pricing)
        try:
            assert cache.get_session_fingerprint(999, "abc") is None
        finally:
            cache.stop()


class TestHotCache:
    def test_store_and_retrieve(self, pricing: PricingService) -> None:
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

    def test_miss_for_different_session(self, pricing: PricingService) -> None:
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

    def test_overwrite_replaces(self, pricing: PricingService) -> None:
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


class TestThreadSafety:
    def test_concurrent_reads_dont_crash(self, pricing: PricingService) -> None:
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

    def test_stop_terminates_poll_thread(self, pricing: PricingService) -> None:
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


class TestIncrementalUpdates:
    def test_oserror_handled_gracefully_during_poll(
        self,
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
