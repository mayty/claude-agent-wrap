# This file has been created with the assistance of an AI tool.
"""Tests for the logs database's own shipped migration scripts."""

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import StorageError
from agent_wrap.infrastructure.constants import (
    INFRASTRUCTURE_DIR,
    MIGRATIONS_DIRNAME,
    Databases,
)
from agent_wrap.infrastructure.logs.repositories.usage import USAGE_CELLS

# The shipped schema, read directly so a throwaway copy of it stays in step with the
# database the rest of this module migrates through the connection factory.
_LOGS_MIGRATION = (
    INFRASTRUCTURE_DIR / Databases.LOGS / MIGRATIONS_DIRNAME / "0001_create_log_index.sql"
)

if TYPE_CHECKING:
    from agent_wrap.containers import Core

EXPECTED_SESSION_COLUMNS = {
    "id",
    "project_hash",
    "provider",
    "claude_session_id",
    "messages_offset",
    "strings_offset",
    "last_ingested_at",
    "record_count",
    "last_event_at_us",
    "models",
    "alias",
    "title",
}

EXPECTED_BLOB_COLUMNS = {"id", "sha256", "codec", "payload"}

EXPECTED_REQUEST_COLUMNS = {
    "id",
    "session_id",
    "ordinal",
    "status",
    "model",
    "usage_source",
    "started_at_us",
    "first_token_at_us",
    "ended_at_us",
    "agent_id",
    "finish_reason",
    "max_tokens",
    "error",
    "message_refs",
    "system_blob",
    "tools_blob",
    "response_blob",
    "input_tokens",
    "output_tokens",
    "cache_write_tokens",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
}

# The aggregate every `agent stats` window runs, reduced to the shape whose plan matters.
# Kept in step with `UsageRepository`'s own statement by
# `test_the_pinned_aggregate_matches_the_one_the_repository_runs`.
USAGE_AGGREGATE = """
    SELECT started_at_us / 3600000000 AS hb, session_id, model, usage_source,
           COUNT(*), MAX(started_at_us),
           SUM(input_tokens), SUM(output_tokens), SUM(cache_write_tokens),
           SUM(cache_write_5m), SUM(cache_write_1h), SUM(cache_read)
      FROM requests
     WHERE status = 'success'
       AND model != ''
       AND (:lo IS NULL OR started_at_us / 3600000000 >= :lo)
       AND (:hi IS NULL OR started_at_us / 3600000000 <= :hi)
  GROUP BY hb, session_id, model, usage_source
"""

# The same aggregate with the raw `started_at_us` term removed from the index, used to
# show what the index looks like without it. Not a variant anything runs.
_AGGREGATE_WITHOUT_RAW_INSTANT = USAGE_AGGREGATE.replace("MAX(started_at_us),", "")


def _seed_session(connection: sqlite3.Connection, session_id: int = 1) -> None:
    """Insert one parent session row so `requests` has something to reference."""
    connection.execute(
        "INSERT INTO sessions (id, project_hash, provider, claude_session_id, last_ingested_at)"
        " VALUES (?, 'hash', 'litellm-bedrock', 'sess', 0)",
        (session_id,),
    )


def _insert_request(connection: sqlite3.Connection, **overrides: object) -> None:
    """Insert a minimal valid `requests` row, with *overrides* applied."""
    row: dict[str, object] = {
        "session_id": 1,
        "ordinal": 0,
        "status": "success",
        "model": "m",
        "usage_source": "native",
        "message_refs": b"",
        "cache_write_tokens": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(f":{name}" for name in row)
    # S608: the column names are this function's own literals and every value is bound.
    # Only the row's *shape* varies, which is the whole point of the helper.
    sql = f"INSERT INTO requests ({columns}) VALUES ({placeholders})"  # noqa: S608
    connection.execute(sql, row)


def test_shipped_migrations_reach_the_head_version(db_core: Core) -> None:
    """Constructing the factory is what migrates, so this asserts the scripts apply."""
    with db_core.logs_db.ro() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize(
    ("table", "expected"),
    [
        ("sessions", EXPECTED_SESSION_COLUMNS),
        ("blobs", EXPECTED_BLOB_COLUMNS),
        ("requests", EXPECTED_REQUEST_COLUMNS),
    ],
)
def test_table_has_the_expected_shape(db_core: Core, table: str, expected: set[str]) -> None:
    with db_core.logs_db.ro() as connection:
        columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    assert columns == expected


def test_logs_database_uses_its_own_persistent_pragmas(db_core: Core) -> None:
    """
    Both settings are silently ignored once the first page exists.

    So this is not a restatement of the constant -- it is the only evidence that the
    tuple ran in an order that let them take effect before the schema was created.
    """
    with db_core.logs_db.ro() as connection:
        assert connection.execute("PRAGMA page_size").fetchone()[0] == 8192
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 2  # INCREMENTAL
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_projects_database_keeps_the_shared_pragmas(db_core: Core) -> None:
    """The logs settings must not leak into the registry, which is a fresh 4 KB file."""
    with db_core.projects_db.ro() as connection:
        assert connection.execute("PRAGMA page_size").fetchone()[0] == 4096
        assert connection.execute("PRAGMA auto_vacuum").fetchone()[0] == 0


def test_usage_aggregate_is_served_entirely_by_its_covering_index(db_core: Core) -> None:
    """
    The stats query must never open `requests` itself.

    If a future column addition drops the index from covering, the query still returns
    the right answer while quietly reading the wide `message_refs` blob for every row.
    Only a plan assertion catches that — and it has to insist on the word COVERING. A
    plain "USING INDEX" means SQLite is seeking the table row per index entry, which on
    this table is exactly the cost the index exists to avoid.
    """
    with db_core.logs_db.ro() as connection:
        plan = [
            row["detail"]
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {USAGE_AGGREGATE}", {"lo": None, "hi": None}
            )
        ]
    assert plan == ["SCAN requests USING COVERING INDEX idx_requests_usage"]


def test_the_indexed_expression_alone_does_not_cover_the_aggregate() -> None:
    """
    Why the index stores `started_at_us` raw as well as bucketed.

    SQLite will not satisfy a selected ``started_at_us / 3600000000`` from the indexed
    expression: it re-reads the column off the table row, so the index covers the query
    only because the raw value is in it too. That is not obvious from reading the
    schema, and without the raw term the plan silently drops to a per-row table seek —
    which is how it shipped before this test existed.

    Demonstrated on a throwaway copy of the real schema, because the real index has the
    term and the point is what happens to one that does not. The schema is the shipped
    migration rather than a hand-written table, so a future column change cannot leave
    this passing against a shape the database no longer has.
    """
    connection = sqlite3.connect(":memory:")
    connection.executescript(_LOGS_MIGRATION.read_text(encoding="utf-8"))
    connection.execute("DROP INDEX idx_requests_usage")
    connection.execute(
        "CREATE INDEX no_raw_instant ON requests ("
        "  (started_at_us / 3600000000), session_id, model, usage_source,"
        "  input_tokens, output_tokens, cache_write_tokens,"
        "  cache_write_5m, cache_write_1h, cache_read"
        ") WHERE status = 'success'"
    )
    plan = [
        row[3]
        for row in connection.execute(
            f"EXPLAIN QUERY PLAN {_AGGREGATE_WITHOUT_RAW_INSTANT}", {"lo": None, "hi": None}
        )
    ]
    connection.close()

    assert plan == ["SCAN requests USING INDEX no_raw_instant"]


def test_the_pinned_aggregate_matches_the_one_the_repository_runs(db_core: Core) -> None:
    """
    The plan above is only worth anything if it is the real query's plan.

    Compared by plan rather than by text so formatting differences do not fail it, and
    asserted here because the statement lives in the repository while the index lives
    in the migration — nothing else would notice the two drifting apart.
    """
    with db_core.logs_db.ro() as connection:
        pinned = [
            row["detail"]
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {USAGE_AGGREGATE}", {"lo": None, "hi": None}
            )
        ]
        real = [
            row["detail"]
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {USAGE_CELLS}", {"lo": None, "hi": None}
            )
        ]

    assert real == pinned


def test_deleting_a_session_cascades_to_its_requests(db_core: Core) -> None:
    factory = db_core.logs_db
    with factory.enable_writes():
        with factory.rw() as connection:
            _seed_session(connection)
            _insert_request(connection)
        with factory.rw() as connection:
            connection.execute("DELETE FROM sessions")

    with factory.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"] == 0


def test_duplicate_ordinal_in_a_session_is_rejected(db_core: Core) -> None:
    """
    A collision means ingest misaligned against its watermark.

    There is deliberately no ON CONFLICT on this table, so the bug surfaces instead of
    being swallowed. `match` matters: WritesNotEnabledError is a StorageError too, so a
    bare raises() would also pass on a missing grant.
    """
    factory = db_core.logs_db
    with factory.enable_writes():
        with factory.rw() as connection:
            _seed_session(connection)
            _insert_request(connection, ordinal=7)

        with pytest.raises(StorageError, match="write failed"), factory.rw() as connection:
            _insert_request(connection, ordinal=7)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("sha256", b"too-short"),
        ("codec", "gzip"),
    ],
)
def test_blob_check_constraints_fire(db_core: Core, column: str, value: object) -> None:
    """A 32-byte digest and a known codec are both schema-enforced, not conventions."""
    row: dict[str, object] = {"sha256": b"0" * 32, "codec": "raw", "payload": b"x"}
    row[column] = value
    factory = db_core.logs_db
    with (
        factory.enable_writes(),
        pytest.raises(StorageError, match="write failed"),
        factory.rw() as connection,
    ):
        connection.execute(
            "INSERT INTO blobs (sha256, codec, payload) VALUES (:sha256, :codec, :payload)", row
        )


def test_cache_split_cannot_exceed_the_total_it_splits(db_core: Core) -> None:
    factory = db_core.logs_db
    with factory.enable_writes():
        with factory.rw() as connection:
            _seed_session(connection)

        with pytest.raises(StorageError, match="write failed"), factory.rw() as connection:
            _insert_request(connection, cache_write_tokens=10, cache_write_5m=8, cache_write_1h=5)

        # The same row with a split that fits is accepted.
        with factory.rw() as connection:
            _insert_request(connection, cache_write_tokens=10, cache_write_5m=5, cache_write_1h=5)


def test_tables_are_strict(db_core: Core) -> None:
    """STRICT keeps a text token count from creeping into an INTEGER column."""
    factory = db_core.logs_db
    with factory.enable_writes():
        with factory.rw() as connection:
            _seed_session(connection)

        with pytest.raises(StorageError, match="write failed"), factory.rw() as connection:
            _insert_request(connection, input_tokens="lots")
