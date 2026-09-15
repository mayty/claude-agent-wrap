# This file has been created with the assistance of an AI tool.
"""Tests for LogsService's ingest pass over the whole log tree."""

import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.constants import LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.constants import INGEST_LOCK_NAME, MAX_CHUNK_RECORDS
from agent_wrap.domain.logs.models import RetentionScope
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.exceptions import StorageError
from agent_wrap.lib.flock import lock_and_hold

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

    from agent_wrap.containers import Core
    from agent_wrap.infrastructure.logs.models import BlobSweep
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository


@pytest.fixture
def logs_root(tmp_path: Path) -> Path:
    """
    Return the central log tree the service walks.

    ``TOOL_DIR`` is redirected to ``tmp_path`` by the autouse ``_patch_path_constants``
    fixture, so this is the same path ``ingest_tree`` will compose for itself.
    """
    root = tmp_path / LITELLM_LOGS_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def ingest_svc(
    display_mock: Mock,
    log_ingest_repository: LogIngestRepository,
    log_session_repository: SessionRepository,
    log_request_repository: RequestRepository,
) -> LogsService:
    """Return a LogsService over a real, empty logs database in ``tmp_path``."""
    return LogsService(
        pricing_service=Mock(spec=PricingService),
        stats_service=Mock(spec=StatsService),
        config_service=Mock(spec=ConfigService),
        display_service=display_mock,
        log_ingest_repository=log_ingest_repository,
        log_session_repository=log_session_repository,
        log_request_repository=log_request_repository,
    )


def _record(**overrides: Any) -> dict[str, Any]:
    """Build a minimal success record in the `request.body` shape."""
    rec: dict[str, Any] = {
        "timing": {"start": 1000.5, "end": 1001.25},
        "status": "success",
        "model": "bedrock/claude-sonnet-4",
        "request": {"body": {"messages": [{"role": "user", "content": "hi"}]}},
        "response": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    rec.update(overrides)
    return rec


def _write_session(
    logs_root: Path,
    project_hash: str,
    provider: str,
    session_id: str,
    records: list[dict[str, Any]],
) -> Path:
    """Append *records* to one session's messages file, creating the tree as needed."""
    session_dir = logs_root / project_hash / provider / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / "messages.jsonl").open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")
    return session_dir


def _count(db_core: Core, table: str) -> int:
    """Count rows in *table*, through a read-only connection of its own."""
    with db_core.logs_db.ro() as connection:
        statement = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 -- literal table names
        return connection.execute(statement).fetchone()["n"]


@pytest.mark.usefixtures("logs_root")
def test_an_empty_tree_reports_nothing_seen(ingest_svc: LogsService) -> None:
    report = ingest_svc.ingest_tree()

    assert report is not None
    assert report.sessions_seen == 0
    assert report.records_ingested == 0
    assert report.ok


def test_every_session_in_the_tree_is_ingested(
    ingest_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record(), _record()])
    _write_session(logs_root, "hash-b", "dashscope", "sess-2", [_record()])

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert (report.sessions_seen, report.sessions_changed) == (2, 2)
    assert report.records_ingested == 3
    assert _count(db_core, "requests") == 3
    assert _count(db_core, "sessions") == 2


def test_a_second_pass_over_an_unchanged_tree_ingests_nothing(
    ingest_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    Idempotency, and the state a warm host is in on every pass but the first.

    ``sessions_seen`` still counts the session while ``sessions_changed`` drops to zero
    -- that is what distinguishes "already up to date" from "found nothing", and the
    verb reports both figures for exactly that reason.
    """
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])
    ingest_svc.ingest_tree()

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert (report.sessions_seen, report.sessions_changed) == (1, 0)
    assert report.records_ingested == 0
    assert _count(db_core, "requests") == 1


def test_only_records_appended_since_the_last_pass_are_read(
    ingest_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record(), _record()])
    ingest_svc.ingest_tree()
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert report.records_ingested == 1
    assert _count(db_core, "requests") == 3


def test_a_session_appearing_between_passes_is_picked_up(
    ingest_svc: LogsService, logs_root: Path
) -> None:
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])
    ingest_svc.ingest_tree()
    _write_session(logs_root, "hash-b", "bedrock", "sess-2", [_record()])

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert (report.sessions_seen, report.sessions_changed) == (2, 1)


def test_a_rewritten_log_file_is_re_read_from_the_start(
    ingest_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    Truncation recovery. A shorter file was replaced, so every stored offset is a lie.

    The session is forgotten and read again rather than resumed, because there is no
    way to know which existing rows still correspond to file content.
    """
    session_dir = _write_session(
        logs_root, "hash-a", "bedrock", "sess-1", [_record(), _record(), _record()]
    )
    ingest_svc.ingest_tree()
    with (session_dir / "messages.jsonl").open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(_record()) + "\n")

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert report.sessions_reset == 1
    assert report.records_ingested == 1
    assert _count(db_core, "requests") == 1


def test_a_session_deleted_between_passes_leaves_the_others_alone(
    ingest_svc: LogsService, logs_root: Path
) -> None:
    """
    A directory removed out of band -- by `agent cleanup` -- is simply not walked.

    Its rows are removed by cleanup's own delete rather than by ingest, so this asserts
    only that the walk copes: no failure, and the surviving session still reported.
    """
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])
    session_dir = _write_session(logs_root, "hash-b", "bedrock", "sess-2", [_record()])
    ingest_svc.ingest_tree()
    (session_dir / "messages.jsonl").unlink()
    session_dir.rmdir()

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert report.sessions_seen == 1
    assert report.ok


def test_a_session_spanning_several_chunks_is_fully_ingested(
    ingest_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    More records than one chunk holds, so the service applies several per session.

    Worth asserting through the service rather than the parser: the cross-chunk blob
    reference that broke the first real run only exists once more than one chunk is
    applied to the same session.
    """
    count = MAX_CHUNK_RECORDS + 5
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record() for _ in range(count)])

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert report.records_ingested == count
    assert _count(db_core, "requests") == count


def test_one_failing_session_does_not_abandon_the_others(
    ingest_svc: LogsService,
    logs_root: Path,
    log_ingest_repository: LogIngestRepository,
    mocker: MockerFixture,
) -> None:
    """
    A backfill spanning hundreds of sessions must not be lost to one bad directory.

    The failure is still collected and named, which is what makes the verb exit
    non-zero -- it is reported rather than suppressed.
    """
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])
    _write_session(logs_root, "hash-b", "bedrock", "sess-2", [_record()])
    _write_session(logs_root, "hash-c", "bedrock", "sess-3", [_record()])

    real = log_ingest_repository.ingest_chunk
    calls: list[str] = []

    def failing_ingest(key: Any, chunk: Any) -> None:
        calls.append(key.project_hash)
        if key.project_hash == "hash-b":
            msg = "write failed: disk on fire"
            raise StorageError(msg)
        real(key, chunk)

    mocker.patch.object(log_ingest_repository, "ingest_chunk", side_effect=failing_ingest)

    report = ingest_svc.ingest_tree()

    assert report is not None
    assert calls == ["hash-a", "hash-b", "hash-c"]
    assert not report.ok
    assert [path.name for path, _ in report.failed] == ["sess-2"]
    assert "disk on fire" in report.failed[0][1]
    assert report.records_ingested == 2


def test_a_held_lock_makes_the_pass_a_no_op(
    ingest_svc: LogsService, logs_root: Path, db_core: Core, tmp_path: Path
) -> None:
    """
    Contention returns None and reads nothing at all.

    Two processes ingesting one tree is not a queue to join: whoever holds the lock is
    already doing the work this pass was about to do, and doing it twice would only
    make both contend on the single database transaction instead.
    """
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])
    # Composed from tmp_path rather than imported: `_patch_path_constants` rewrites the
    # constant inside the service's module, not the one this test module bound at import.
    handle = lock_and_hold(tmp_path / ".agent-launches" / INGEST_LOCK_NAME)
    assert handle is not None
    try:
        assert ingest_svc.ingest_tree() is None
    finally:
        handle.release()

    assert _count(db_core, "requests") == 0


def test_the_lock_is_released_for_the_next_pass(ingest_svc: LogsService, logs_root: Path) -> None:
    """A completed pass must not leave the lock held, or the next one would find it busy."""
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])

    assert ingest_svc.ingest_tree() is not None
    assert ingest_svc.ingest_tree() is not None


def _sweep(service: LogsService) -> BlobSweep | None:
    """
    Reclaim with retention switched off, which is what a bare sweep now is.

    The two run under one lock and in one order -- retention first, so the rows it
    drops are gone before the sweep judges their content -- so there is no method that
    sweeps alone. An empty scope is how a caller asks for only the second half.
    """
    reclaim = service.reclaim_index(RetentionScope(days=0, sessions=()))
    return None if reclaim is None else reclaim.sweep


def test_a_sweep_after_a_delete_reclaims_that_project_only(
    ingest_svc: LogsService,
    logs_root: Path,
    db_core: Core,
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    The `agent cleanup` sequence end to end: ingest, forget a project, sweep.

    Two sessions ingested from real files, so the content is whatever the parser
    actually stored rather than a hand-built blob -- which is the only way this asserts
    that the ids the sweep follows are the ones ingest wrote.
    """
    # Distinct content per session, deliberately: the blob store is content-addressed,
    # so two sessions sending the same message share one row and deleting either would
    # free nothing at all.
    for project, text in (("hash-a", "only a's"), ("hash-b", "only b's")):
        message = {"role": "user", "content": text}
        _write_session(
            logs_root,
            project,
            "bedrock",
            f"sess-{project}",
            [_record(request={"body": {"messages": [message]}})],
        )
    assert ingest_svc.ingest_tree() is not None
    before = _count(db_core, "blobs")

    log_ingest_repository.delete_projects(["hash-a"])
    sweep = _sweep(ingest_svc)

    assert sweep is not None
    assert 0 < sweep.removed < before, "only the deleted project's content goes"
    assert sweep.freed_bytes > 0
    assert _count(db_core, "blobs") == before - sweep.removed


def test_a_sweep_leaves_a_fully_referenced_index_alone(
    ingest_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    Nothing is unreachable in an index nothing has been deleted from.

    The interned strings are the case that makes this worth asserting against real
    files: no request names one, so a sweep that followed ids alone would delete the
    long message this session's record quotes.
    """
    long_message = {"role": "user", "content": "x" * 200}
    _write_session(
        logs_root,
        "hash-a",
        "bedrock",
        "sess-1",
        [_record(request={"body": {"messages": [long_message]}})],
    )
    assert ingest_svc.ingest_tree() is not None
    before = _count(db_core, "blobs")

    sweep = _sweep(ingest_svc)

    assert sweep is not None
    assert sweep == (0, 0)
    assert _count(db_core, "blobs") == before


def test_a_held_lock_makes_the_sweep_a_no_op(
    ingest_svc: LogsService,
    logs_root: Path,
    db_core: Core,
    tmp_path: Path,
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    Contention returns None and deletes nothing.

    Sharper here than for an ingest pass: the sweep decides what is unreachable in one
    transaction and deletes it in another, so an ingest committing between the two
    could leave a request pointing at content that has just gone.
    """
    _write_session(logs_root, "hash-a", "bedrock", "sess-1", [_record()])
    assert ingest_svc.ingest_tree() is not None
    log_ingest_repository.delete_projects(["hash-a"])
    before = _count(db_core, "blobs")

    handle = lock_and_hold(tmp_path / ".agent-launches" / INGEST_LOCK_NAME)
    assert handle is not None
    try:
        assert _sweep(ingest_svc) is None
    finally:
        handle.release()

    assert _count(db_core, "blobs") == before
