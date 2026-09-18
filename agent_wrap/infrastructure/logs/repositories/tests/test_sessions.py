# This file has been created with the assistance of an AI tool.
"""
Tests for the session read side: what the viewer's list endpoints are served from.

Rows are inserted directly, because what is under test is the SQL and the decoding on
the way out — not how ingest arrives at the values, which is ``test_ingest.py``'s
subject. The merge into the single session a user sees is not here either: it is
read-time policy and lives in ``domain/logs/listing.py``.
"""

import json
from typing import TYPE_CHECKING

import pytest

from agent_wrap.infrastructure.logs.models import Revision, SessionKey
from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository

if TYPE_CHECKING:
    import sqlite3

    from agent_wrap.containers import Core


@pytest.fixture
def sessions(db_core: Core) -> SessionRepository:
    return SessionRepository(connection_factory=db_core.logs_db)


def _insert(connection: sqlite3.Connection, **overrides: object) -> None:
    """Insert one ``sessions`` row, defaulting to a plausible ingested session."""
    row: dict[str, object] = {
        "project_hash": "hashA",
        "provider": "litellm-bedrock",
        "claude_session_id": "sess-1",
        "last_ingested_at": 1_000,
        "record_count": 3,
        "last_event_at_us": 1_784_275_200_000_000,
        "models": json.dumps(["claude-opus-4-8"]),
        "alias": None,
        "title": None,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    # S608: the column names are this function's own literals and every value is bound.
    sql = f"INSERT INTO sessions ({columns}) VALUES ({placeholders})"  # noqa: S608
    connection.execute(sql, row)


def _seed(db_core: Core, **overrides: object) -> None:
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _insert(connection, **overrides)


def test_an_empty_table_has_no_revision(sessions: SessionRepository) -> None:
    """
    A host that has never ingested reports None rather than 0.

    The distinction is what the viewer's refresh gate compares against: its own "not
    known yet" is also None, and a zero would make the first pass look like a match and
    skip the only rebuild that had anything to do.
    """
    assert sessions.revision() == Revision(last_ingested_ns=None, count=0)


def test_the_revision_reports_the_newest_ingest_and_the_row_count(
    db_core: Core, sessions: SessionRepository
) -> None:
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _insert(connection, claude_session_id="sess-1", last_ingested_at=1_000)
        _insert(connection, claude_session_id="sess-2", last_ingested_at=9_000)
        _insert(connection, claude_session_id="sess-3", last_ingested_at=5_000)

    assert sessions.revision() == Revision(last_ingested_ns=9_000, count=3)


def test_sessions_returns_every_row_with_its_key(
    db_core: Core, sessions: SessionRepository
) -> None:
    factory = db_core.logs_db
    with factory.enable_writes(), factory.rw() as connection:
        _insert(connection, project_hash="hashA", claude_session_id="sess-1")
        _insert(connection, project_hash="hashB", claude_session_id="sess-2")

    keys = {row.key for row in sessions.sessions()}

    assert keys == {
        SessionKey("hashA", "litellm-bedrock", "sess-1"),
        SessionKey("hashB", "litellm-bedrock", "sess-2"),
    }


def test_a_row_carries_the_five_summary_fields(db_core: Core, sessions: SessionRepository) -> None:
    """The columns that replaced ``meta.json``, decoded into what the list renders."""
    _seed(
        db_core,
        record_count=7,
        last_event_at_us=1_784_275_200_123_456,
        models=json.dumps(["claude-opus-4-8", "claude-haiku-4-5"]),
        alias="fixing-the-parser",
        title="Fix the parser",
    )

    (row,) = sessions.sessions()

    assert row.record_count == 7
    assert row.last_event_at_us == 1_784_275_200_123_456
    assert row.models == ("claude-opus-4-8", "claude-haiku-4-5")
    assert (row.alias, row.title) == ("fixing-the-parser", "Fix the parser")


def test_models_arrive_decoded_rather_than_as_json(
    db_core: Core, sessions: SessionRepository
) -> None:
    """
    The JSON encoding stops at the storage boundary.

    Stored as a string because nothing queries into it, but a caller handed the string
    would have to know that -- and the one caller is a template that would happily
    render ``["m"]``.
    """
    _seed(db_core, models=json.dumps(["m"]))

    assert sessions.sessions()[0].models == ("m",)


def test_a_session_with_no_timed_record_reports_no_instant(
    db_core: Core, sessions: SessionRepository
) -> None:
    """``last_event_at_us`` is nullable, and an all-failure session is where it is null."""
    _seed(db_core, last_event_at_us=None)

    assert sessions.sessions()[0].last_event_at_us is None


def test_an_empty_table_lists_nothing(sessions: SessionRepository) -> None:
    assert sessions.sessions() == []


def test_an_empty_index_still_reports_its_size(sessions: SessionRepository) -> None:
    """
    A migrated database has pages; only ``last_ingested_ns`` distinguishes it from a
    used one, which is what `agent inspect` reports as "never ingested".
    """
    footprint = sessions.footprint()
    assert footprint.database_bytes > 0
    assert (footprint.sessions, footprint.requests) == (0, 0)
    assert footprint.last_ingested_ns is None


def test_the_footprint_counts_sessions_and_their_records(
    sessions: SessionRepository, db_core: Core
) -> None:
    """Requests come off the sessions table's own counter, not from COUNT(requests)."""
    _seed(db_core, claude_session_id="sess-1", record_count=3, last_ingested_at=1_000)
    _seed(db_core, claude_session_id="sess-2", record_count=4, last_ingested_at=9_000)

    footprint = sessions.footprint()
    assert (footprint.sessions, footprint.requests) == (2, 7)
    assert footprint.last_ingested_ns == 9_000
