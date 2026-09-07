# This file has been created with the assistance of an AI tool.
"""Tests for the LogsCache — in-memory cache and background FS watcher."""

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest

import agent_wrap.domain.logs.usage_tracker as usage_tracker_mod
import agent_wrap.domain.stats.service as stats_mod
from agent_wrap.domain.config.project_registry import ProjectRegistry
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.logs.io import logs_dir
from agent_wrap.domain.logs.watcher import CacheWatcher
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture

    from agent_wrap.containers import Core


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


@pytest.fixture(autouse=True)
def inert_watcher(mocker: MockerFixture) -> None:
    """
    Keep the watcher's threads out of every test in this module.

    ``reconcile`` and ``apply_paths`` are documented as running on the watcher's single
    consumer thread, and in production they only ever do: ``start`` rebuilds before the
    thread exists, and every other call is the consumer's own. These tests drive both
    from the main thread while a real watcher runs, which breaks that contract -- a
    filesystem write inside a test queues an event, and the consumer can handle the same
    registry delta concurrently with the test's explicit ``reconcile``. Both then
    pass the ``current_paths != _known_project_paths`` gate and insert the same group, so
    ``_group_count`` intermittently read 2 where the test asserted 1.

    Event delivery is covered by ``test_watcher.py``; nothing here depends on it.
    """
    mocker.patch.object(CacheWatcher, "start", autospec=True)


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
def config_svc(projects_repository: ProjectsRepository) -> ConfigService:
    return ConfigService(
        display_service=Mock(spec=DisplayService), projects_repository=projects_repository
    )


@pytest.fixture
def read_only_config_svc(read_only_core: Core) -> ConfigService:
    """Return a ConfigService over a factory holding no write grant — the daemon's own."""
    return ConfigService(
        display_service=Mock(spec=DisplayService),
        projects_repository=ProjectsRepository(connection_factory=read_only_core.projects_db),
    )


def test_the_daemon_cannot_import_the_legacy_registry(
    tmp_path: Path,
    pricing: PricingService,
    read_only_config_svc: ConfigService,
    read_only_core: Core,
    real_stats: StatsService,
) -> None:
    """
    The viewer runs in a process that never takes a write grant.

    A reconcile reads the registry, and an empty table is what would trigger the
    one-time ``projects.txt`` import — so without the gate the daemon would migrate host
    state off a filesystem event. It must come away having written nothing.
    """
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True, exist_ok=True)
    compressed = ProjectRegistry.compress([str(tmp_path / "a"), str(tmp_path / "b")])
    (launches / "projects.txt").write_text("\n".join(compressed) + "\n", encoding="utf-8")

    cache = LogsCache(real_stats, read_only_config_svc, pricing)
    cache.start()
    try:
        assert cache.get_projects() == []
    finally:
        cache.stop()

    with read_only_core.projects_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 0


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
    register_projects: Callable[..., None],
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

    register_projects(project)

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
    """Returns empty lists when nothing is registered."""
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


def test_rebuild_populates_every_structure(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    Startup fills the session cache, all three fingerprint levels, and the manifest.

    ``rebuild`` delegates to ``reconcile`` rather than running its own scan, so this pins
    the equivalence that trade rests on — nothing asserted it before.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    write_session(project, "litellm-deepseek", "sess-2", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        # Compared as a set: both sessions share a last_ts, so their relative order is
        # whatever the directory walk produced.
        assert {s["session_id"] for s in cache.get_sessions(0) or []} == {"sess-1", "sess-2"}
        assert [p["sessions"] for p in cache.get_projects()] == [2]
        assert cache.get_projects_fingerprint()["mtime"] is not None
        assert (cache.get_sessions_fingerprint(0) or {})["mtime"] is not None
        assert (cache.get_session_fingerprint(0, "sess-1") or {})["mtime"] is not None
        assert len(cache._known_messages) == 2
    finally:
        cache.stop()


def test_rebuild_and_reconcile_agree(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """A reconcile straight after startup changes nothing — the regression guard."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        before = (
            cache.get_projects(),
            cache.get_projects_fingerprint(),
            cache.get_sessions(0),
            cache.get_sessions_fingerprint(0),
            cache.get_session_fingerprint(0, "sess-1"),
            dict(cache._known_messages),
        )
        cache.reconcile()
        assert (
            cache.get_projects(),
            cache.get_projects_fingerprint(),
            cache.get_sessions(0),
            cache.get_sessions_fingerprint(0),
            cache.get_session_fingerprint(0, "sess-1"),
            dict(cache._known_messages),
        ) == before
    finally:
        cache.stop()


def test_rebuild_counts_a_group_shared_session_once(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    A session shared by two members of one group counts once at startup.

    The old startup pass counted session *directories* per member and summed them, so it
    reported two where the sessions view renders one merged row. Every update since has
    counted the merged sessions, so the table's number changed after the first event.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    runs = tmp_path / "runs"
    runs.mkdir()
    # resolve_group looks for the marker as a *file*.
    (runs / ".agent_stats_leaf").write_text("", encoding="utf-8")
    member_a = runs / "agent-a"
    member_b = runs / "agent-b"
    write_session(member_a, "litellm-bedrock", "shared-sess", [valid_record])
    write_session(member_b, "litellm-bedrock", "shared-sess", [valid_record])
    register_projects(member_a, member_b)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        assert _group_count(cache) == 1
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["shared-sess"]
        assert [p["sessions"] for p in cache.get_projects()] == [1]
    finally:
        cache.stop()


def test_projects_fingerprint_reflects_the_pass_that_published_it(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    The published projects marker is derived from the *current* session fingerprints.

    It sums ``_sessions_fp``, so computing it before that dict is refreshed publishes a
    marker one pass behind. That is not a lost notification — consecutive recomputations
    always straddle a change, so the marker still moves — but it means the value served
    disagrees with the cache it claims to summarize, which is a trap for anything that
    later compares the two.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        write_session(project, "litellm-bedrock", "sess-2", [valid_record])
        cache.reconcile()

        assert cache.get_projects_fingerprint() == cache._recompute_projects_fp_from_cache()
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
        "last_ts": 1.0,
        "models": ["a"],
        "providers": ["litellm-bedrock"],
    }
    started_cache._merge_combined(
        existing,
        {
            "provider": "litellm-bedrock",
            "count": 1,
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


def test_reads_still_work_after_stop(started_cache: LogsCache) -> None:
    started_cache.stop()
    assert isinstance(started_cache.get_projects(), list)


def test_stop_before_start_reports_that_nothing_is_running(
    pricing: PricingService, config_mock: ConfigService
) -> None:
    stats = Mock(spec=StatsService)
    cache = LogsCache(stats, config_mock, pricing)
    with pytest.raises(RuntimeError, match="ThreadNotRunning"):
        cache.stop()


def test_reconcile_survives_an_oserror_while_scanning(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    mocker: MockerFixture,
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    A failing stat leaves reconcile harmless and the cache readable.

    Drives ``reconcile`` directly rather than waiting for the watcher to schedule
    one: the contract under test is the cache's, and asserting it synchronously
    makes the test both exact and instant.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    (project / ".claude").mkdir(parents=True)
    logs_target = tmp_path / "litellm-logs" / "abc"
    logs_target.mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(logs_target, target_is_directory=True)
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.rebuild()

    original_iterdir = Path.iterdir
    _simulated_msg = "Simulated"

    def _failing_iterdir(self_path: Path) -> Generator[Path]:
        if self_path == logs_target:
            raise OSError(_simulated_msg)
        yield from original_iterdir(self_path)

    mocker.patch("pathlib.Path.iterdir", _failing_iterdir)

    cache.reconcile()

    assert isinstance(cache.get_logs_dirs(0), list)


def test_every_manifest_path_resolves_to_its_session(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    Every manifest key maps to its (pid, sid) by path arithmetic alone.

    The manifest used to carry a ``path_to_key`` side-table built in the same walk, so
    this was true by construction. Now ``_diff_manifest`` recovers the mapping from
    ``_root_to_pid``, and a path it could not attribute would be silently skipped — the
    session would go stale with nothing to show for it. Hence an explicit invariant.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    register_projects(proj_a, proj_b)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        manifest = cache._gather_directory_manifest()
        assert manifest

        expected: dict[Path, tuple[int, str]] = {}
        for pid in range(_group_count(cache)):
            for group_logs_dir in cache.get_logs_dirs(pid) or []:
                for provider_dir in group_logs_dir.iterdir():
                    for session_dir in provider_dir.iterdir():
                        expected[session_dir / "messages.jsonl"] = (pid, session_dir.name)

        assert {p: cache._resolve_message_path(p) for p in manifest} == expected
    finally:
        cache.stop()


def test_changed_session_resolved_via_manifest_diff(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """A modified messages.jsonl is re-scanned and reflected in cached sessions."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        sessions = cache.get_sessions(0)
        assert sessions is not None
        assert sessions[0]["count"] == 1

        with (sdir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(valid_record) + "\n")
        (sdir / "meta.json").unlink(missing_ok=True)

        cache.reconcile()

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
    register_projects: Callable[..., None],
) -> None:
    """A session whose directory vanishes out of band is dropped on the next reconcile."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        assert cache.get_session_fingerprint(0, "sess-1") is not None

        (sdir / "messages.jsonl").unlink()
        (sdir / "meta.json").unlink(missing_ok=True)
        sdir.rmdir()

        cache.reconcile()

        sessions = cache.get_sessions(0)
        assert sessions == []
        assert cache.get_session_fingerprint(0, "sess-1") is None
    finally:
        cache.stop()


def test_rebuild_writes_usage_json_before_any_update_runs(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    The statusline reads usage.json, and updates are event-driven.

    Left to the first update, that file would not exist until a heartbeat up to a
    minute after startup, so a host that had never run a viewer would show no totals
    at all in the meantime.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    write_session(
        project,
        "litellm-bedrock",
        "sess-1",
        [{**valid_record, "timing": {"start": time.time(), "end": time.time()}}],
    )
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.rebuild()

    assert (tmp_path / ".claude" / "usage.json").is_file()


def test_apply_paths_rescans_only_the_named_session(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """The fast path picks up an append without walking the tree."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    write_session(project, "litellm-bedrock", "sess-2", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.rebuild()

    with (sdir / "messages.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(valid_record) + "\n")
    (sdir / "meta.json").unlink(missing_ok=True)

    cache.apply_paths({sdir / "messages.jsonl"})

    by_id = {s["session_id"]: s for s in cache.get_sessions(0) or []}
    assert by_id["sess-1"]["count"] == 2
    assert by_id["sess-2"]["count"] == 1


def test_apply_paths_leaves_the_same_manifest_as_reconcile(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    The consistency rule the two update paths rest on.

    Both must leave ``_known_messages`` identical, or a later reconcile would
    re-report as changed whatever the fast path had already applied.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)
    messages = sdir / "messages.jsonl"

    fast = LogsCache(real_stats, config_svc, pricing)
    fast.rebuild()
    slow = LogsCache(real_stats, config_svc, pricing)
    slow.rebuild()

    with messages.open("a", encoding="utf-8") as f:
        f.write(json.dumps(valid_record) + "\n")
    (sdir / "meta.json").unlink(missing_ok=True)

    fast.apply_paths({messages})
    # Both scans must start from the same on-disk state. scan_session_meta seeds a
    # meta.json cache as a side effect, so without this the second cache would read
    # back the first one's cache rather than rescanning, and the comparison would be
    # measuring that instead of the two update paths.
    (sdir / "meta.json").unlink(missing_ok=True)
    slow.reconcile()

    assert fast._known_messages == slow._known_messages
    assert fast.get_sessions(0) == slow.get_sessions(0)
    assert fast.get_session_fingerprint(0, "sess-1") == slow.get_session_fingerprint(0, "sess-1")
    assert fast.get_projects_fingerprint() == slow.get_projects_fingerprint()


def test_apply_paths_drops_a_session_whose_record_file_is_gone(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    sdir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)
    messages = sdir / "messages.jsonl"

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.rebuild()

    messages.unlink()
    (sdir / "meta.json").unlink(missing_ok=True)

    cache.apply_paths({messages})

    assert cache.get_sessions(0) == []
    assert cache.get_session_fingerprint(0, "sess-1") is None
    assert messages not in cache._known_messages


def test_apply_paths_falls_back_to_reconcile_for_an_unmappable_path(
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """
    A log dir belonging to no known group has to widen to the full pass.

    This is what makes a newly registered project visible: its first session write is a
    path under the watched tree that no cached group owns, and the reconcile it forces
    is what re-reads the registry. Nothing else watches the registry, so dropping such a
    path instead would leave the project invisible until the next heartbeat.
    """
    stats_mod.TOOL_DIR = tmp_path
    unregistered = tmp_path / "litellm-logs" / "hashX" / "litellm-bedrock" / "sess-x"
    unregistered.mkdir(parents=True)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.rebuild()
    spy = mocker.spy(cache, "reconcile")

    cache.apply_paths({unregistered / "messages.jsonl"})

    spy.assert_called_once_with()


def test_apply_paths_widens_to_reconcile_across_a_day_rollover(
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    real_stats: StatsService,
    mocker: MockerFixture,
) -> None:
    """A rollover has to re-offer every tracked file, not just this batch's."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.rebuild()
    mocker.patch.object(cache._usage_tracker, "detect_rollover", return_value=True)
    # Spied after start(): rebuild() delegates to reconcile, so an earlier spy would
    # already have that call on it and assert_called_once_with would fail.
    spy = mocker.spy(cache, "reconcile")

    cache.apply_paths({tmp_path / "a" / "b" / "c" / "messages.jsonl"})

    spy.assert_called_once_with()


@pytest.mark.parametrize(
    ("spelling", "filename", "expected"),
    [
        pytest.param("as-listed", "messages.jsonl", (0, "sess-1"), id="symlinked-spelling"),
        pytest.param("resolved", "messages.jsonl", (0, "sess-1"), id="resolved-spelling"),
        pytest.param("outside", "messages.jsonl", None, id="outside-all-groups"),
        pytest.param("as-listed", "meta.json", None, id="not-a-record-file"),
    ],
)
def test_resolve_message_path(
    tmp_path: Path,
    started_cache: LogsCache,
    spelling: str,
    filename: str,
    expected: tuple[int, str] | None,
) -> None:
    """
    Both spellings of a group's logs dir resolve to the same project.

    A group lists its ``.claude/litellm-logs`` symlink, which is what a directory walk
    produces; a filesystem event names the real dir under the shared tree. One mapping has
    to answer for both, or the fast path silently misses every event.
    """
    real = tmp_path / "litellm-logs" / "abc"
    real.mkdir(parents=True)
    link = tmp_path / "proj" / ".claude" / "litellm-logs"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)

    started_cache._groups = [{"root": link, "name": "g", "paths": [], "logs_dirs": [link]}]
    started_cache._reindex_roots()

    root = {"as-listed": link, "resolved": real, "outside": tmp_path / "elsewhere"}[spelling]
    path = root / "litellm-bedrock" / "sess-1" / filename

    assert started_cache._resolve_message_path(path) == expected


def test_reconcile_writes_usage_json(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """After reconcile, usage.json exists with totals from today's records."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "testproj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        cache.reconcile()

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


def test_usage_tracker_responds_to_file_changes(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """Adding records to a session file increases usage totals on the next reconcile."""
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
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        # First pass — seed the tracker.
        cache.reconcile()
        usage_path = tmp_path / ".claude" / "usage.json"
        initial = json.loads(usage_path.read_text(encoding="utf-8"))
        assert initial["requests"] == 1

        # Append a second record and force a rescan.
        with (sdir / "messages.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(today_record) + "\n")
        (sdir / "meta.json").unlink(missing_ok=True)

        cache.reconcile()
        updated = json.loads(usage_path.read_text(encoding="utf-8"))
        assert updated["requests"] > initial["requests"]
    finally:
        cache.stop()


def test_reconcile_rewrites_stale_usage_json_when_no_activity(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """A day with no records overwrites a previous day's payload instead of touching it."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    yesterday = time.time() - 36 * 3600
    project = tmp_path / "testproj"
    old_record = {
        "status": "success",
        "model": "m",
        "timing": {"start": yesterday},
        "response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
    }
    sdir = write_session(project, "litellm-bedrock", "sess-1", [old_record])
    os.utime(sdir / "messages.jsonl", (yesterday, yesterday))
    register_projects(project)

    # Yesterday's payload, as a previous daemon run would have left it behind.
    usage_path = tmp_path / ".claude" / "usage.json"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(
        json.dumps(
            {
                "in": 100,
                "out": 50,
                "cache": 0,
                "cache_creation": 0,
                "cost": "$1.23",
                "requests": 1,
            }
        ),
        encoding="utf-8",
    )

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        cache.reconcile()

        data = json.loads(usage_path.read_text(encoding="utf-8"))
        assert data["in"] == 0
        assert data["out"] == 0
        assert data["requests"] == 0
        assert data["cost"] == "$0.00"
    finally:
        cache.stop()


def test_reconcile_zeroes_usage_json_after_day_rollover(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
) -> None:
    """A rollover past the day boundary with no new records zeroes the payload."""
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
    write_session(project, "litellm-bedrock", "sess-1", [today_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    usage_path = tmp_path / ".claude" / "usage.json"
    try:
        cache.reconcile()
        assert json.loads(usage_path.read_text(encoding="utf-8"))["requests"] == 1

        # Move the tracker's clock a day forward, leaving the log file untouched —
        # the daemon's first tick past the day boundary on an idle machine.
        fake_datetime = mocker.patch.object(usage_tracker_mod, "datetime", autospec=True)
        fake_datetime.now.return_value = datetime.now(UTC) + timedelta(days=1)
        fake_datetime.fromtimestamp.side_effect = datetime.fromtimestamp

        cache.reconcile()

        data = json.loads(usage_path.read_text(encoding="utf-8"))
        assert data["in"] == 0
        assert data["out"] == 0
        assert data["requests"] == 0
        assert data["cost"] == "$0.00"
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
    register_projects: Callable[..., None],
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
    register_projects(proj_a)
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


def test_a_project_whose_logs_dir_appears_late_is_picked_up(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
) -> None:
    """
    A registered path with no logs dir yet is retried, not written off.

    ``link_litellm_logs`` normally runs before ``record_project``, but it is best-effort
    and swallows OSError, so a path can land in the registry before its logs dir exists.
    Recording it as known would strand it: no later registry write can put it back in
    ``added``, so the project stayed invisible until the viewer restarted.
    """
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    project = tmp_path / "proj"
    project.mkdir()
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        assert _group_count(cache) == 0

        # The logs dir shows up later; the registry is deliberately left untouched.
        write_session(project, "litellm-bedrock", "sess-1", [valid_record])
        cache.reconcile()

        assert _group_count(cache) == 1
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-1"]
    finally:
        cache.stop()


def test_a_same_content_registry_rewrite_does_not_reprocess(  # noqa: PLR0913
    tmp_path: Path,
    pricing: PricingService,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[[Path, str, str, list[dict[str, Any]]], Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
) -> None:
    """``record_project`` re-records on every launch; contents decide, not the revision."""
    stats_mod.TOOL_DIR = tmp_path

    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = LogsCache(real_stats, config_svc, pricing)
    cache.start()
    try:
        spy = mocker.spy(cache, "_merge_added_paths")
        register_projects(project)  # a re-launch from the same directory
        cache.reconcile()

        spy.assert_not_called()
        # The registry's revision still reaches the browser-facing fingerprint.
        assert cache._registry_last_change == config_svc.registry_fingerprint().last_change
    finally:
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
    """Both additions and removals in the same registry change are handled."""
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
    # Simulate _handle_registry_change: prune + merge.
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
    register_projects: Callable[..., None],
) -> None:
    """A path added via reconcile is merged incrementally, without a full rebuild."""
    stats_mod.TOOL_DIR = tmp_path
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True)

    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    register_projects(proj_a)

    cache = LogsCache(real_stats, config_svc, pricing)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a])
    cache.start()

    rebuild_spy = mocker.spy(cache, "rebuild")

    # Simulate project B being added and a reconcile detecting it.
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b])
    register_projects(proj_a, proj_b)
    cache.reconcile()

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
