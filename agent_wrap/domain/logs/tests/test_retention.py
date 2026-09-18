# This file has been created with the assistance of an AI tool.
"""
Tests for age-based retention over the log tree and the index together.

Retention is the one thing in this codebase that deletes a request nobody asked it to
delete, so almost every case here is about what it must *not* take: a session too new, a
session with no date, a session the index has not finished reading, and every session at
all when ``AGENT_LOGS_RETENTION_DAYS`` is unset — which is the default.

The log files go with the index rows, deliberately. Keeping the files would make
retention undo itself on the next pass, which re-reads any directory the index has
forgotten from byte zero.
"""

import json
import time
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.constants import LITELLM_LOGS_DIRNAME
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.logs.constants import (
    INGEST_LOCK_NAME,
    RETENTION_DAYS_ENV,
    SECONDS_PER_DAY,
)
from agent_wrap.domain.logs.service import LogsService
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService
from agent_wrap.lib.flock import lock_and_hold

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.containers import Core
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository

# Far enough back that no plausible clock skew makes it recent, and dated by the record
# rather than by ingest: the row's last_ingested_at is stamped with now either way.
LONG_AGO = time.time() - 400 * SECONDS_PER_DAY


@pytest.fixture
def logs_root(tmp_path: Path) -> Path:
    """Return the central log tree the service walks, with ``TOOL_DIR`` redirected."""
    root = tmp_path / LITELLM_LOGS_DIRNAME
    root.mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def logs_svc(
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


@pytest.fixture
def retention_90(monkeypatch: pytest.MonkeyPatch) -> None:
    """Switch retention on at 90 days for one test."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, "90")


def _record(ended: float, *, text: str = "hi") -> dict[str, Any]:
    """Build a success record whose ``timing.end`` is *ended*."""
    return {
        "timing": {"start": ended - 1.0, "end": ended},
        "status": "success",
        "model": "bedrock/claude-sonnet-4",
        "request": {"body": {"messages": [{"role": "user", "content": text}]}},
        "response": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }


def _write_session(
    logs_root: Path, project_hash: str, session_id: str, records: list[dict[str, Any]]
) -> Path:
    """Append *records* to one session's messages file, creating the tree as needed."""
    session_dir = logs_root / project_hash / "bedrock" / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / "messages.jsonl").open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return session_dir


def _count(db_core: Core, table: str) -> int:
    with db_core.logs_db.ro() as connection:
        statement = f"SELECT COUNT(*) AS n FROM {table}"  # noqa: S608 -- literal table names
        return connection.execute(statement).fetchone()["n"]


def test_retention_is_off_when_the_variable_is_unset(logs_svc: LogsService) -> None:
    """The default, and the one this whole feature is arranged around."""
    assert logs_svc.retention_days() == 0


@pytest.mark.parametrize("raw", ["", "   ", "0"])
def test_an_empty_or_zero_setting_reads_as_off(
    logs_svc: LogsService, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """Exporting the variable with no value reads as clearing it, not as asking for 0 days."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, raw)

    assert logs_svc.retention_days() == 0


@pytest.mark.parametrize("raw", ["ninety", "-1", "9.5"])
def test_a_malformed_setting_raises_rather_than_falling_back(
    logs_svc: LogsService, monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A silent default here would delete on an age nobody chose."""
    monkeypatch.setenv(RETENTION_DAYS_ENV, raw)

    with pytest.raises(ValueError, match=RETENTION_DAYS_ENV):
        logs_svc.retention_days()


@pytest.mark.usefixtures("logs_root")
def test_a_scope_with_retention_off_is_empty_and_says_why(logs_svc: LogsService) -> None:
    """``days == 0`` is what tells "switched off" from "nothing is old enough yet"."""
    scope = logs_svc.retention_scope()

    assert scope.days == 0
    assert scope.is_empty


@pytest.mark.usefixtures("retention_90")
def test_an_old_session_is_surveyed_with_its_size_and_watermark(
    logs_svc: LogsService, logs_root: Path
) -> None:
    session_dir = _write_session(logs_root, "hash-a", "sess-1", [_record(LONG_AGO)])
    assert logs_svc.ingest_tree() is not None

    scope = logs_svc.retention_scope()

    assert scope.days == 90
    assert len(scope.sessions) == 1
    expired = scope.sessions[0]
    assert expired.path == session_dir
    assert expired.key.claude_session_id == "sess-1"
    assert expired.messages_offset == (session_dir / "messages.jsonl").stat().st_size
    assert scope.freed_estimate == expired.size_bytes > 0


@pytest.mark.usefixtures("retention_90")
def test_a_recent_session_is_not_expired(logs_svc: LogsService, logs_root: Path) -> None:
    _write_session(logs_root, "hash-a", "sess-1", [_record(time.time())])
    assert logs_svc.ingest_tree() is not None

    assert logs_svc.retention_scope().is_empty


@pytest.mark.usefixtures("retention_90")
def test_the_newest_record_dates_a_session_not_the_oldest(
    logs_svc: LogsService, logs_root: Path
) -> None:
    """A long-running session is as new as its last request, however old its first was."""
    _write_session(logs_root, "hash-a", "sess-1", [_record(LONG_AGO), _record(time.time())])
    assert logs_svc.ingest_tree() is not None

    assert logs_svc.retention_scope().is_empty


@pytest.mark.usefixtures("retention_90")
def test_an_undated_session_is_never_expired(logs_svc: LogsService, logs_root: Path) -> None:
    """
    Every record lacking a timing leaves nothing to compare an age against.

    Retention deletes files, so the answer to "how old is this" being unknown has to
    mean "leave it", not "assume the worst".
    """
    undated = _record(LONG_AGO)
    del undated["timing"]
    _write_session(logs_root, "hash-a", "sess-1", [undated])
    assert logs_svc.ingest_tree() is not None

    assert logs_svc.retention_scope().is_empty


@pytest.mark.usefixtures("retention_90")
def test_a_session_with_unread_bytes_is_not_expired(logs_svc: LogsService, logs_root: Path) -> None:
    """
    Old enough is not sufficient: the index must have read the whole file first.

    Deleting here would destroy requests no consumer has ever seen, and the appended
    record is newer than the age that selected the session in the first place.
    """
    session_dir = _write_session(logs_root, "hash-a", "sess-1", [_record(LONG_AGO)])
    assert logs_svc.ingest_tree() is not None
    with (session_dir / "messages.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(time.time())) + "\n")

    assert logs_svc.retention_scope().is_empty


@pytest.mark.usefixtures("retention_90")
def test_reclaiming_deletes_the_files_the_rows_and_the_content(
    logs_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    The whole point, end to end: files, rows and blobs all go together.

    Two sessions with deliberately distinct message text, because the blob store is
    content-addressed -- identical messages would collapse to one row that the surviving
    session still reaches, and the sweep would correctly reclaim nothing.
    """
    old_dir = _write_session(logs_root, "hash-a", "sess-old", [_record(LONG_AGO, text="a" * 200)])
    _write_session(logs_root, "hash-b", "sess-new", [_record(time.time(), text="b" * 200)])
    assert logs_svc.ingest_tree() is not None
    before = _count(db_core, "blobs")

    scope = logs_svc.retention_scope()
    reclaim = logs_svc.reclaim_index(scope)

    assert reclaim is not None
    assert reclaim.retention == (1, scope.freed_estimate)
    assert not old_dir.exists()
    assert _count(db_core, "sessions") == 1
    assert reclaim.sweep.removed > 0
    assert _count(db_core, "blobs") == before - reclaim.sweep.removed


@pytest.mark.usefixtures("retention_90")
def test_reclaiming_prunes_the_directories_the_delete_leaves_empty(
    logs_svc: LogsService, logs_root: Path
) -> None:
    """
    An emptied provider and project directory go too; a project with a survivor stays.

    ``rmdir`` refusing a non-empty directory is the whole test -- there is no separate
    emptiness check that could disagree with the filesystem.
    """
    _write_session(logs_root, "hash-a", "sess-old", [_record(LONG_AGO, text="a" * 200)])
    _write_session(logs_root, "hash-b", "sess-old", [_record(LONG_AGO, text="b" * 200)])
    _write_session(logs_root, "hash-b", "sess-new", [_record(time.time(), text="c" * 200)])
    assert logs_svc.ingest_tree() is not None

    assert logs_svc.reclaim_index(logs_svc.retention_scope()) is not None

    assert not (logs_root / "hash-a").exists()
    assert (logs_root / "hash-b" / "bedrock" / "sess-new").is_dir()
    assert logs_root.is_dir(), "the walk stops at the log root"


@pytest.mark.usefixtures("retention_90")
def test_a_session_that_grew_after_the_survey_is_left_alone(
    logs_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    The watermark check is re-asked under the lock, not trusted from the survey.

    That is what makes "retention never deletes an unread request" a property of the
    delete rather than something that was true a few seconds earlier.
    """
    session_dir = _write_session(logs_root, "hash-a", "sess-1", [_record(LONG_AGO)])
    assert logs_svc.ingest_tree() is not None
    scope = logs_svc.retention_scope()
    assert len(scope.sessions) == 1
    with (session_dir / "messages.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_record(time.time())) + "\n")

    reclaim = logs_svc.reclaim_index(scope)

    assert reclaim is not None
    assert reclaim.retention == (0, 0)
    assert session_dir.is_dir()
    assert _count(db_core, "sessions") == 1


@pytest.mark.usefixtures("retention_90")
def test_a_held_lock_makes_the_whole_reclaim_a_no_op(
    logs_svc: LogsService, logs_root: Path, tmp_path: Path, db_core: Core
) -> None:
    """
    Both halves skip together, because both halves are one lock.

    Retention deleting files while an ingest pass reads them is exactly what this
    excludes, and the sweep's own reason is sharper still -- see ``reclaim_index``.
    """
    session_dir = _write_session(logs_root, "hash-a", "sess-1", [_record(LONG_AGO)])
    assert logs_svc.ingest_tree() is not None
    scope = logs_svc.retention_scope()

    handle = lock_and_hold(tmp_path / ".agent-launches" / INGEST_LOCK_NAME)
    assert handle is not None
    try:
        assert logs_svc.reclaim_index(scope) is None
    finally:
        handle.release()

    assert session_dir.is_dir()
    assert _count(db_core, "sessions") == 1


@pytest.mark.usefixtures("retention_90")
def test_a_pruned_session_is_not_re_ingested_by_the_next_pass(
    logs_svc: LogsService, logs_root: Path, db_core: Core
) -> None:
    """
    Retention has to hold, and it only holds because the files went with the rows.

    Dropping the row alone would leave a directory the very next walk reads from byte
    zero -- retention undone, and paid for twice.
    """
    _write_session(logs_root, "hash-a", "sess-1", [_record(LONG_AGO)])
    assert logs_svc.ingest_tree() is not None
    assert logs_svc.reclaim_index(logs_svc.retention_scope()) is not None

    report = logs_svc.ingest_tree()

    assert report is not None
    assert (report.sessions_seen, report.records_ingested) == (0, 0)
    assert _count(db_core, "sessions") == 0
