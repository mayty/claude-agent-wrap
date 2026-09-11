# This file has been created with the assistance of an AI tool.
"""
Tests for the LogsCache — the viewer's in-memory snapshot of the request index.

Every test here writes a session into the central log tree exactly as a sidecar does,
symlinks a project at it, and lets the cache's own ingest pass read it. That is not
scaffolding: the cache derives its lists from ``logs.db`` and from nothing else, so a
test that wrote a log file the index never saw would be asserting an empty cache.
"""

import json
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
from agent_wrap.domain.logs.constants import MESSAGES_FILENAME
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.logs.watcher import CacheWatcher
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.infrastructure.projects.repositories.projects import ProjectsRepository
from agent_wrap.lib.path_hash import project_path_hash

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from pytest_mock import MockerFixture

    from agent_wrap.containers import Core
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository


def _group_count(cache: LogsCache) -> int:
    """
    Count cached groups through the public accessor.

    ``get_project_hashes`` returns None past the last group, so walking upward from 0
    yields the group count without reaching into ``_groups``.
    """
    count = 0
    while cache.get_project_hashes(count) is not None:
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
    """Return a minimal record ingest can parse into one indexed request."""
    return {
        "timing": {"start": 1.0, "completionStart": None, "end": 1.0},
        "response": {},
        "model": "m",
    }


@pytest.fixture
def write_session(tmp_path: Path) -> Callable[..., Path]:
    """
    Return a factory writing a session the way a sidecar does, and linking a project.

    The real layout, and the only one the cache can see anything through: the sidecars
    append under ``TOOL_DIR/litellm-logs/<project hash>/<provider>/<session>/`` and each
    project reaches its own slice by a ``.claude/litellm-logs`` symlink. Appending to an
    existing session is what a second call with the same ids does, since the log files
    are append-only and the ingest watermarks depend on that.
    """

    def _write(
        project: Path,
        provider: str,
        session_id: str,
        records: list[dict[str, Any]],
    ) -> Path:
        central = tmp_path / "litellm-logs" / project_path_hash(project)
        session_dir = central / provider / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / MESSAGES_FILENAME).open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        link = project / ".claude" / "litellm-logs"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            link.symlink_to(central, target_is_directory=True)
        return session_dir

    return _write


@pytest.fixture
def make_cache(
    tmp_path: Path,
    log_session_repository: SessionRepository,
    log_request_repository: RequestRepository,
    log_ingest_repository: LogIngestRepository,
    display_mock: Mock,
) -> Callable[..., LogsCache]:
    """
    Return a factory building a LogsCache wired as the daemon wires it.

    Both halves are the daemon's: the read side it serves its lists from, and the ingest
    pass that fills it. ``ingest=False`` builds the other legal shape — a cache with no
    ingest pass, which is what an embedder gets and which must still serve whatever the
    index already holds.
    """
    logs = LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
        config_service=Mock(spec=ConfigService),
        display_service=display_mock,
        log_ingest_repository=log_ingest_repository,
        log_session_repository=log_session_repository,
        log_request_repository=log_request_repository,
    )
    # The daemon's own directory, which holds the ingest lock. Always present in
    # production; created here because a test may not have registered a project.
    (tmp_path / ".agent-launches").mkdir(parents=True, exist_ok=True)

    def _make(
        stats: StatsService,
        config: ConfigService,
        *,
        ingest: bool = True,
    ) -> LogsCache:
        return LogsCache(
            stats,
            config,
            log_session_repository,
            ingest=logs.ingest_tree if ingest else None,
        )

    return _make


def _pricing_mock() -> PricingService:
    """
    Return a PricingService mock whose bucket factories build real Buckets.

    Not optional even for a test that ignores pricing: every reconcile ends in a
    ``usage.json`` flush, which sums buckets and serializes the result, and a Mock
    bucket is not JSON-serializable. So the three factory methods have to be real
    wherever a cache is started at all.
    """
    mock = Mock(spec=PricingService)
    mock.new_bucket.side_effect = Bucket
    mock.merged_bucket.side_effect = Bucket.merged
    mock.bucket_from_usage.side_effect = _bucket_from_usage
    mock.normalize_model.side_effect = lambda m: m  # pyrefly: ignore [implicit-any-lambda]
    mock.request_cache_ttl.return_value = None
    mock.extract_usage.return_value = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
    }
    mock.compute_cost.return_value = 0.001
    return mock


def _bucket_from_usage(usage: Any, *, msgs: int, unrecorded: int = 0) -> Bucket:
    """Stand in for ``PricingService.bucket_from_usage`` on a mocked pricing service."""
    bucket = Bucket()
    bucket.add(usage, 0.0)
    bucket.msgs = msgs
    bucket.unrecorded = unrecorded
    return bucket


@pytest.fixture
def pricing() -> PricingService:
    return _pricing_mock()


@pytest.fixture
def isolated_stats(
    tmp_path: Path, make_stats_service: Callable[[PricingService], StatsService]
) -> StatsService:
    stats_mod.TOOL_DIR = tmp_path
    (tmp_path / ".agent-launches").mkdir(parents=True, exist_ok=True)
    return make_stats_service(_pricing_mock())


@pytest.fixture
def real_stats(
    pricing: PricingService, make_stats_service: Callable[[PricingService], StatsService]
) -> StatsService:
    """Return a StatsService backed by the real (mock-priced) PricingService."""
    return make_stats_service(pricing)


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
    read_only_config_svc: ConfigService,
    read_only_core: Core,
    real_stats: StatsService,
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    The viewer runs in a process that never takes a projects-database write grant.

    A reconcile reads the registry, and an empty table is what would trigger the
    one-time ``projects.txt`` import — so without the gate the daemon would migrate host
    state off a filesystem event. It must come away having written nothing.
    """
    launches = tmp_path / ".agent-launches"
    launches.mkdir(parents=True, exist_ok=True)
    compressed = ProjectRegistry.compress([str(tmp_path / "a"), str(tmp_path / "b")])
    (launches / "projects.txt").write_text("\n".join(compressed) + "\n", encoding="utf-8")

    cache = make_cache(real_stats, read_only_config_svc)
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
def started_cache(
    make_cache: Callable[..., LogsCache],
    config_mock: ConfigService,
) -> Generator[LogsCache]:
    stats = Mock(spec=StatsService)
    stats.resolve_group.return_value = (_CWD, ".", False)
    stats.orphaned_log_dirs.return_value = cast("list[Path]", [])
    # Every reconcile ends in a usage.json flush, which asks the stats service for the
    # day's total and serializes it -- so a mocked one has to hand back a real Bucket.
    stats.day_totals.return_value = Bucket()
    cache = make_cache(stats, config_mock)
    cache.start()
    try:
        yield cache
    finally:
        cache.stop()


def test_cache_populated_when_registry_exists(
    tmp_path: Path,
    config_svc: ConfigService,
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """A project with a litellm-logs symlink becomes a group, sessions or not."""
    project = tmp_path / "testproj"
    (project / ".claude").mkdir(parents=True)
    logs_target = tmp_path / "litellm-logs" / "abc"
    logs_target.mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(logs_target, target_is_directory=True)

    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert _group_count(cache) == 1
        # No indexed sessions yet, so the project is a group with nothing to render and
        # is left out of the projects list.
        assert cache.get_projects() == []
        assert cache.get_sessions(0) == []
    finally:
        cache.stop()


def test_cache_empty_when_no_registry(
    tmp_path: Path,
    config_svc: ConfigService,
    real_stats: StatsService,
    make_cache: Callable[..., LogsCache],
) -> None:
    """Returns empty lists when nothing is registered."""
    stats_mod.TOOL_DIR = tmp_path / "nonexistent"
    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert _group_count(cache) == 0
        assert cache.get_projects() == []
        assert cache.get_projects_fingerprint() == {"rev": None, "count": 0}
    finally:
        cache.stop()


def test_rebuild_populates_every_structure(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    Startup fills the session cache and all three fingerprint levels.

    ``rebuild`` delegates to ``reconcile`` rather than running its own pass, so this pins
    the equivalence that trade rests on — nothing asserted it before.
    """
    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    write_session(project, "litellm-deepseek", "sess-2", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        # Compared as a set: both sessions share a last_ts, so their relative order is
        # whatever the merge produced from equal keys.
        assert {s["session_id"] for s in cache.get_sessions(0) or []} == {"sess-1", "sess-2"}
        assert [p["sessions"] for p in cache.get_projects()] == [2]
        assert cache.get_projects_fingerprint()["rev"] is not None
        assert (cache.get_sessions_fingerprint(0) or {})["rev"] is not None
        assert (cache.get_session_fingerprint(0, "sess-1") or {})["rev"] is not None
    finally:
        cache.stop()


def test_rebuild_and_reconcile_agree(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """A reconcile straight after startup changes nothing — the regression guard."""
    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        before = (
            cache.get_projects(),
            cache.get_projects_fingerprint(),
            cache.get_sessions(0),
            cache.get_sessions_fingerprint(0),
            cache.get_session_fingerprint(0, "sess-1"),
        )
        cache.reconcile()
        assert (
            cache.get_projects(),
            cache.get_projects_fingerprint(),
            cache.get_sessions(0),
            cache.get_sessions_fingerprint(0),
            cache.get_session_fingerprint(0, "sess-1"),
        ) == before
    finally:
        cache.stop()


def test_rebuild_counts_a_group_shared_session_once(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A session shared by two members of one group counts once at startup.

    Each member owns its own project hash, so the index holds two rows — and the number
    the projects table shows has to be the merged sessions the drill-down renders, not
    the directories behind them.
    """
    runs = tmp_path / "runs"
    runs.mkdir()
    # resolve_group looks for the marker as a *file*.
    (runs / ".agent_stats_leaf").write_text("", encoding="utf-8")
    member_a = runs / "agent-a"
    member_b = runs / "agent-b"
    write_session(member_a, "litellm-bedrock", "shared-sess", [valid_record])
    write_session(member_b, "litellm-bedrock", "shared-sess", [valid_record])
    register_projects(member_a, member_b)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert _group_count(cache) == 1
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["shared-sess"]
        assert [p["sessions"] for p in cache.get_projects()] == [1]
    finally:
        cache.stop()


def test_every_indexed_session_reaches_the_group_that_owns_it(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
    log_session_repository: SessionRepository,
) -> None:
    """
    The join between a group and the index is by project hash, and it has to be exact.

    The index knows a project only by the hash the sidecar wrote its directory under; a
    group knows a ``.claude/litellm-logs`` symlink. A group whose hashes came out wrong
    would list nothing at all, with no error to say why — and the sessions would still
    be in the database. So this asserts the two agree on the whole population.
    """
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    register_projects(proj_a, proj_b)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        listed = {
            meta["session_id"]
            for pid in range(_group_count(cache))
            for meta in cache.get_sessions(pid) or []
        }
        indexed = {row.key.claude_session_id for row in log_session_repository.sessions()}

        assert indexed == {"sess-a", "sess-b"}
        assert listed == indexed
    finally:
        cache.stop()


def test_a_project_whose_logs_live_outside_the_central_tree_claims_no_hash(
    tmp_path: Path,
    config_svc: ConfigService,
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A hash is taken only from a directory the central tree actually holds.

    Otherwise a project whose symlink points somewhere unexpected would claim whatever
    that directory happens to be named — and a name colliding with a real hash would
    show another project's sessions under this one.
    """
    project = tmp_path / "proj"
    elsewhere = tmp_path / "not-the-tree" / "hashA"
    elsewhere.mkdir(parents=True)
    (project / ".claude").mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(elsewhere, target_is_directory=True)
    # A real session under the hash the stray link is named after.
    genuine = tmp_path / "litellm-logs" / "hashA" / "litellm-bedrock" / "sess-1"
    genuine.mkdir(parents=True)
    (genuine / MESSAGES_FILENAME).write_text(
        json.dumps({"timing": {"start": 1.0, "end": 1.0}, "response": {}, "model": "m"}) + "\n",
        encoding="utf-8",
    )
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert cache.get_sessions(0) == []
    finally:
        cache.stop()


def test_projects_fingerprint_reflects_the_pass_that_published_it(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    The published projects marker is derived from the *current* session fingerprints.

    It sums them, so computing it before that dict is refreshed publishes a marker one
    pass behind. That is not a lost notification — consecutive recomputations always
    straddle a change, so the marker still moves — but it means the value served
    disagrees with the cache it claims to summarize, which is a trap for anything that
    later compares the two.
    """
    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        write_session(project, "litellm-bedrock", "sess-2", [valid_record])
        cache.reconcile()

        assert cache.get_projects_fingerprint() == cache._recompute_projects_fp_from_cache()
    finally:
        cache.stop()


def test_every_accessor_returns_none_for_an_unknown_project(
    started_cache: LogsCache,
) -> None:
    """An unknown project id is None everywhere, which is what the server answers 400 on."""
    assert started_cache.get_project_hashes(999) is None
    assert started_cache.get_sessions(999) is None
    assert started_cache.get_sessions_fingerprint(999) is None


def test_get_session_fingerprint_unknown(
    started_cache: LogsCache,
) -> None:
    assert started_cache.get_session_fingerprint(999, "abc") is None


def test_concurrent_reads_dont_crash(started_cache: LogsCache) -> None:
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            for _ in range(100):
                started_cache.get_project_hashes(0)
                started_cache.get_projects()
                started_cache.get_projects_fingerprint()
                started_cache.get_session_meta(0, "x")
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
    make_cache: Callable[..., LogsCache],
    config_mock: ConfigService,
) -> None:
    cache = make_cache(Mock(spec=StatsService), config_mock)
    with pytest.raises(RuntimeError, match="ThreadNotRunning"):
        cache.stop()


def test_reconcile_survives_an_oserror_while_reading_the_tree(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    mocker: MockerFixture,
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    An unreadable log directory leaves reconcile harmless and the cache readable.

    Drives ``reconcile`` directly rather than waiting for the watcher to schedule
    one: the contract under test is the cache's, and asserting it synchronously
    makes the test both exact and instant.
    """
    project = tmp_path / "testproj"
    (project / ".claude").mkdir(parents=True)
    logs_target = tmp_path / "litellm-logs" / "abc"
    logs_target.mkdir(parents=True)
    (project / ".claude" / "litellm-logs").symlink_to(logs_target, target_is_directory=True)
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.rebuild()

    original_iterdir = Path.iterdir
    _simulated_msg = "Simulated"

    def _failing_iterdir(self_path: Path) -> Generator[Path]:
        if self_path == logs_target:
            raise OSError(_simulated_msg)
        yield from original_iterdir(self_path)

    mocker.patch("pathlib.Path.iterdir", _failing_iterdir)

    cache.reconcile()

    assert isinstance(cache.get_project_hashes(0), list)


def test_an_appended_record_raises_the_sessions_count(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """The whole chain on one tick: append, ingest, re-derive, serve."""
    project = tmp_path / "testproj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        sessions = cache.get_sessions(0)
        assert sessions is not None
        assert sessions[0]["count"] == 1

        write_session(project, "litellm-bedrock", "sess-1", [valid_record])
        cache.reconcile()

        sessions = cache.get_sessions(0)
        assert sessions is not None
        assert sessions[0]["count"] == 2
    finally:
        cache.stop()


def test_a_project_whose_rows_are_deleted_stops_being_listed(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    Forgetting a project in the index is what unlists its sessions.

    This is ``agent cleanup``'s half of the deletion: it removes the directory and the
    rows together, and the rows are what the viewer reads. Both the session and its
    fingerprint go, and the project leaves the projects list with them.
    """
    project = tmp_path / "testproj"
    session_dir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert cache.get_session_fingerprint(0, "sess-1") is not None

        # The directory goes first, exactly as `agent cleanup` orders it -- otherwise the
        # next ingest pass would read it all straight back in.
        (session_dir / MESSAGES_FILENAME).unlink()
        session_dir.rmdir()
        log_ingest_repository.delete_projects([project_path_hash(project)])

        cache.reconcile()

        assert cache.get_sessions(0) == []
        assert cache.get_session_fingerprint(0, "sess-1") is None
        assert cache.get_projects() == []
    finally:
        cache.stop()


def test_a_deleted_directory_alone_leaves_its_session_listed(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    The index is the source of the lists, so removing files behind its back changes nothing.

    A deliberate change from the walk this replaced, which dropped a session as soon as
    its record file vanished. The rows are the record now: they hold everything the
    viewer renders, so a session whose log file someone deleted by hand is still
    readable, and it is ``agent cleanup`` — which deletes rows and directory together —
    that makes it go.
    """
    project = tmp_path / "testproj"
    session_dir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        (session_dir / MESSAGES_FILENAME).unlink()
        session_dir.rmdir()

        cache.reconcile()

        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-1"]
    finally:
        cache.stop()


def test_rebuild_writes_usage_json_before_any_update_runs(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    The statusline reads usage.json, and updates are event-driven.

    Left to the first update, that file would not exist until a heartbeat up to a
    minute after startup, so a host that had never run a viewer would show no totals
    at all in the meantime.
    """
    project = tmp_path / "testproj"
    write_session(
        project,
        "litellm-bedrock",
        "sess-1",
        [{**valid_record, "timing": {"start": time.time(), "end": time.time()}}],
    )
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.rebuild()

    assert (tmp_path / ".claude" / "usage.json").is_file()


def test_apply_paths_picks_up_an_append(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """The event-driven path reaches the same served state as a full pass."""
    project = tmp_path / "testproj"
    session_dir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    write_session(project, "litellm-bedrock", "sess-2", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.rebuild()

    write_session(project, "litellm-bedrock", "sess-1", [valid_record])

    cache.apply_paths({session_dir / MESSAGES_FILENAME})

    by_id = {s["session_id"]: s for s in cache.get_sessions(0) or []}
    assert by_id["sess-1"]["count"] == 2
    assert by_id["sess-2"]["count"] == 1


def test_apply_paths_leaves_the_same_state_as_reconcile(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    The two update paths agree on everything they publish.

    They differ only in whether the registry is re-read, and both derive their lists
    from the whole index — so a divergence here would mean the fast path was serving
    something the complete pass would not.
    """
    project = tmp_path / "testproj"
    session_dir = write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)
    messages = session_dir / MESSAGES_FILENAME

    fast = make_cache(real_stats, config_svc)
    fast.rebuild()
    slow = make_cache(real_stats, config_svc)
    slow.rebuild()

    write_session(project, "litellm-bedrock", "sess-1", [valid_record])

    fast.apply_paths({messages})
    slow.reconcile()

    assert fast.get_sessions(0) == slow.get_sessions(0)
    assert fast.get_session_fingerprint(0, "sess-1") == slow.get_session_fingerprint(0, "sess-1")
    assert fast.get_projects_fingerprint() == slow.get_projects_fingerprint()


def test_apply_paths_falls_back_to_reconcile_for_an_unmappable_path(
    tmp_path: Path,
    config_svc: ConfigService,
    real_stats: StatsService,
    mocker: MockerFixture,
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A log dir belonging to no known group has to widen to the full pass.

    This is what makes a newly registered project visible: its first session write is a
    path under the watched tree that no cached group owns, and the reconcile it forces
    is what re-reads the registry. Nothing else watches the registry, so dropping such a
    path instead would leave the project invisible until the next heartbeat.
    """
    unregistered = tmp_path / "litellm-logs" / "hashX" / "litellm-bedrock" / "sess-x"
    unregistered.mkdir(parents=True)

    cache = make_cache(real_stats, config_svc)
    cache.rebuild()
    spy = mocker.spy(cache, "reconcile")

    cache.apply_paths({unregistered / MESSAGES_FILENAME})

    spy.assert_called_once_with()


def test_apply_paths_widens_to_reconcile_across_a_day_rollover(
    tmp_path: Path,
    config_svc: ConfigService,
    real_stats: StatsService,
    mocker: MockerFixture,
    make_cache: Callable[..., LogsCache],
) -> None:
    """A rollover changes which day usage.json reports, whatever this batch touched."""
    cache = make_cache(real_stats, config_svc)
    cache.rebuild()
    mocker.patch.object(cache._usage_tracker, "detect_rollover", return_value=True)
    # Spied after rebuild(): it delegates to reconcile, so an earlier spy would already
    # have that call on it and assert_called_once_with would fail.
    spy = mocker.spy(cache, "reconcile")

    cache.apply_paths({tmp_path / "a" / "b" / "c" / MESSAGES_FILENAME})

    spy.assert_called_once_with()


def test_an_unchanged_index_is_not_re_derived(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
    log_session_repository: SessionRepository,
    mocker: MockerFixture,
) -> None:
    """
    A heartbeat that found nothing new rebuilds nothing.

    The revision probe is one B-tree lookup and the rebuild behind it walks every
    indexed session, so on a quiet host — which is most of them, most of the time — the
    gate is the difference between the two. Its effect is invisible in the served data
    by design, so the read itself is what has to be observed.
    """
    project = tmp_path / "testproj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        spy = mocker.spy(log_session_repository, "sessions")

        cache.reconcile()
        assert spy.call_count == 0

        write_session(project, "litellm-bedrock", "sess-2", [valid_record])
        cache.reconcile()
        assert spy.call_count == 1
    finally:
        cache.stop()


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

    A group lists its ``.claude/litellm-logs`` symlink; a filesystem event names the real
    dir under the shared tree. One mapping has to answer for both, or every event widens
    to a full reconcile.
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


def test_reconcile_writes_usage_json(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """After reconcile, usage.json exists with totals from today's records."""
    project = tmp_path / "testproj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        cache.reconcile()

        usage_path = tmp_path / ".claude" / "usage.json"
        assert usage_path.is_file(), f"usage.json not found at {usage_path}"
        data = json.loads(usage_path.read_text(encoding="utf-8"))
        assert set(data) >= {"in", "out", "cache", "cost", "requests"}
    finally:
        cache.stop()


def test_reconcile_ingests_appended_records_before_flushing_usage_json(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    End to end: a request appended to a log file reaches usage.json on the next tick.

    This is the daemon's half of the arrangement that lets every other consumer read
    the index instead of the log files. Nothing else on a normal host ingests, so if
    reconcile stopped doing it the statusline would sit at whatever the last
    ``agent reindex`` had seen — with no error anywhere to say so.
    """
    project = tmp_path / "testproj"
    today_record = {
        "status": "success",
        "model": "m",
        "timing": {"start": time.time()},
        "response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
    }
    write_session(project, "litellm-bedrock", "sess-1", [today_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        usage_path = tmp_path / ".claude" / "usage.json"
        assert json.loads(usage_path.read_text(encoding="utf-8"))["requests"] == 1

        write_session(project, "litellm-bedrock", "sess-1", [today_record])
        cache.reconcile()

        assert json.loads(usage_path.read_text(encoding="utf-8"))["requests"] == 2
    finally:
        cache.stop()


def test_reconcile_without_an_ingest_callable_still_flushes(
    tmp_path: Path,
    config_mock: ConfigService,
    real_stats: StatsService,
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A cache built with no ingest pass is legal, and writes a payload from the index.

    That is the shape a test or an embedder gets, and the reason it must not raise: the
    ingest callable is the daemon's contribution, not a precondition of the cache.
    """
    cache = make_cache(real_stats, config_mock, ingest=False)
    cache.start()
    try:
        data = json.loads((tmp_path / ".claude" / "usage.json").read_text(encoding="utf-8"))
        assert data["requests"] == 0
    finally:
        cache.stop()


def test_reconcile_rewrites_stale_usage_json_when_no_activity(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """A day with no records overwrites a previous day's payload instead of touching it."""
    yesterday = time.time() - 36 * 3600
    project = tmp_path / "testproj"
    old_record = {
        "status": "success",
        "model": "m",
        "timing": {"start": yesterday},
        "response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
    }
    write_session(project, "litellm-bedrock", "sess-1", [old_record])
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

    cache = make_cache(real_stats, config_svc)
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


def test_reconcile_zeroes_usage_json_after_day_rollover(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    write_session: Callable[..., Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """A rollover past the day boundary with no new records zeroes the payload."""
    project = tmp_path / "testproj"
    today_record = {
        "status": "success",
        "model": "m",
        "timing": {"start": time.time()},
        "response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
    }
    write_session(project, "litellm-bedrock", "sess-1", [today_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
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
        assert data["requests"] == 0
    finally:
        cache.stop()


def test_added_project_merged_without_disturbing_the_first(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """Registering a second project leaves the first project's sessions exactly as they were."""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    register_projects(proj_a)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        sessions_a = cache.get_sessions(0)
        assert sessions_a is not None
        assert [s["session_id"] for s in sessions_a] == ["sess-a"]

        register_projects(proj_a, proj_b)
        cache.reconcile()

        # "proj-b" sorts after "proj-a", so it lands at pid 1.
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-a"]
        assert [s["session_id"] for s in cache.get_sessions(1) or []] == ["sess-b"]
    finally:
        cache.stop()


def test_added_path_merges_into_existing_group(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """Two paths sharing a .agent_stats_leaf marker merge into one group, sessions and all."""
    parent = tmp_path / "group"
    parent.mkdir()
    (parent / ".agent_stats_leaf").write_text("my-group", encoding="utf-8")

    proj_a = parent / "sub-a"
    proj_b = parent / "sub-b"
    write_session(proj_a, "litellm-bedrock", "shared-sess", [valid_record])
    register_projects(proj_a)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert _group_count(cache) == 1
        # One hash per member, so the group's hashes track its membership 1:1 -- and they
        # are what a session read is scoped by, so this is the join with the index.
        assert cache.get_project_hashes(0) == [project_path_hash(proj_a)]

        # The second member runs for the first time: its log dir and its registration
        # both appear now. Writing it earlier would have made its unclaimed hash an
        # <orphaned> group at startup, which is a different test's subject.
        second = dict(valid_record)
        second["timing"] = {"start": 2.0, "completionStart": None, "end": 2.0}
        write_session(proj_b, "litellm-bedrock", "shared-sess", [second])
        register_projects(proj_a, proj_b)
        cache.reconcile()

        assert _group_count(cache) == 1
        assert cache.get_project_hashes(0) == [
            project_path_hash(proj_a),
            project_path_hash(proj_b),
        ]

        sessions = cache.get_sessions(0)
        assert sessions is not None
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "shared-sess"
        assert sessions[0]["count"] == 2
        assert sessions[0]["providers"] == ["litellm-bedrock"]
    finally:
        cache.stop()


def test_a_project_whose_logs_dir_appears_late_is_picked_up(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A registered path with no logs dir yet is retried, not written off.

    ``link_litellm_logs`` normally runs before ``record_project``, but it is best-effort
    and swallows OSError, so a path can land in the registry before its logs dir exists.
    Recording it as known would strand it: no later registry write can put it back in
    ``added``, so the project stayed invisible until the viewer restarted.
    """
    project = tmp_path / "proj"
    project.mkdir()
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
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


def test_a_same_content_registry_rewrite_does_not_reprocess(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """``record_project`` re-records on every launch; contents decide, not the revision."""
    project = tmp_path / "proj"
    write_session(project, "litellm-bedrock", "sess-1", [valid_record])
    register_projects(project)

    cache = make_cache(real_stats, config_svc)
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


def test_added_group_inserted_mid_list(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """A new group root sorting between existing roots shifts the ids after it."""
    proj_a = tmp_path / "aaa-proj"
    proj_b = tmp_path / "bbb-proj"
    proj_c = tmp_path / "ccc-proj"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])
    register_projects(proj_a, proj_c)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-a"]
        assert [s["session_id"] for s in cache.get_sessions(1) or []] == ["sess-c"]

        register_projects(proj_a, proj_b, proj_c)
        cache.reconcile()

        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-a"]
        assert [s["session_id"] for s in cache.get_sessions(1) or []] == ["sess-b"]
        assert [s["session_id"] for s in cache.get_sessions(2) or []] == ["sess-c"]
        assert cache.get_sessions_fingerprint(2) is not None
    finally:
        cache.stop()


def test_mixed_add_and_remove_handled_incrementally(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    make_cache: Callable[..., LogsCache],
) -> None:
    """Both additions and removals in the same registry change are handled."""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    proj_c = tmp_path / "proj-c"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])

    cache = make_cache(real_stats, config_svc)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_b])
    cache.start()
    try:
        assert _group_count(cache) == 2

        # Replace proj_a with proj_c, which runs for the first time here.
        write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])
        mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_b, proj_c])
        cache.reconcile()

        assert _group_count(cache) == 2
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-b"]
        assert [s["session_id"] for s in cache.get_sessions(1) or []] == ["sess-c"]
    finally:
        cache.stop()


def test_a_registry_addition_does_not_force_a_full_rebuild(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """A path added since the last pass is merged into the group list, not rebuilt from it."""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_b, "litellm-bedrock", "sess-b", [valid_record])
    register_projects(proj_a)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        rebuild_spy = mocker.spy(cache, "rebuild")

        register_projects(proj_a, proj_b)
        cache.reconcile()

        rebuild_spy.assert_not_called()
        assert [s["session_id"] for s in cache.get_sessions(1) or []] == ["sess-b"]
    finally:
        cache.stop()


def test_a_group_inserted_ahead_of_another_renumbers_its_hashes(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    register_projects: Callable[..., None],
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A project's hashes follow its id when a group is inserted ahead of it.

    A project id is an index into the group list, so an insertion renumbers everything
    after it. The hashes are what the server scopes a session read by, so a mapping left
    behind would answer one project's id with another project's content.
    """
    proj_a = tmp_path / "aaa-proj"
    proj_c = tmp_path / "ccc-proj"
    write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])
    register_projects(proj_c)

    cache = make_cache(real_stats, config_svc)
    cache.start()
    try:
        assert cache.get_project_hashes(0) == [project_path_hash(proj_c)]

        write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
        register_projects(proj_a, proj_c)
        cache.reconcile()

        assert cache.get_project_hashes(0) == [project_path_hash(proj_a)]
        assert cache.get_project_hashes(1) == [project_path_hash(proj_c)]
        assert [s["session_id"] for s in cache.get_sessions(1) or []] == ["sess-c"]
    finally:
        cache.stop()


def test_an_unregistered_project_hands_its_id_to_the_survivor(  # noqa: PLR0913 -- a tree, a registry and three services
    tmp_path: Path,
    config_svc: ConfigService,
    valid_record: dict[str, Any],
    write_session: Callable[..., Path],
    real_stats: StatsService,
    mocker: MockerFixture,
    make_cache: Callable[..., LogsCache],
) -> None:
    """
    A group that is gone renumbers the ones after it, hashes and sessions together.

    The other direction of the same hazard: what sat at pid 1 is now pid 0, and every
    structure keyed by the id has to agree about that in the same pass.
    """
    proj_a = tmp_path / "aaa-proj"
    proj_c = tmp_path / "ccc-proj"
    write_session(proj_a, "litellm-bedrock", "sess-a", [valid_record])
    write_session(proj_c, "litellm-bedrock", "sess-c", [valid_record])

    cache = make_cache(real_stats, config_svc)
    mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_a, proj_c])
    cache.start()
    try:
        assert cache.get_project_hashes(0) == [project_path_hash(proj_a)]

        mocker.patch.object(cache._config, "read_project_paths", return_value=[proj_c])
        cache.reconcile()

        assert cache.get_project_hashes(0) == [project_path_hash(proj_c)]
        assert [s["session_id"] for s in cache.get_sessions(0) or []] == ["sess-c"]
        assert cache.get_project_hashes(1) is None
    finally:
        cache.stop()
