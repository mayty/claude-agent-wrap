# This file has been created with the assistance of an AI tool.
"""Tests for the projects database's own shipped migration scripts."""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import StorageError

if TYPE_CHECKING:
    from agent_wrap.containers import Core

EXPECTED_COLUMNS = {"path", "first_seen_at", "last_seen_at"}


def test_shipped_migrations_reach_the_head_version(db_core: Core) -> None:
    """Constructing the factory is what migrates, so this asserts the scripts apply."""
    with db_core.projects_db.ro() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_projects_table_has_the_expected_shape(db_core: Core) -> None:
    with db_core.projects_db.ro() as connection:
        columns = {row["name"]: row for row in connection.execute("PRAGMA table_info(projects)")}

    assert set(columns) == EXPECTED_COLUMNS
    assert columns["path"]["pk"] == 1
    assert all(columns[name]["notnull"] == 1 for name in EXPECTED_COLUMNS)


def test_duplicate_path_is_rejected(db_core: Core) -> None:
    """Dedup is the schema's job, not the caller's."""
    factory = db_core.projects_db
    with factory.enable_writes():
        with factory.rw() as connection:
            connection.execute("INSERT INTO projects VALUES ('/a', 1, 1)")

        # `match` matters here: WritesNotEnabledError is a StorageError too, so a bare
        # raises() would pass on a missing grant instead of on the constraint.
        with pytest.raises(StorageError, match="write failed"), factory.rw() as connection:
            connection.execute("INSERT INTO projects VALUES ('/a', 2, 2)")

    with factory.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 1


def test_timestamps_reject_non_integers(db_core: Core) -> None:
    """The table is STRICT, so a text timestamp cannot creep in."""
    factory = db_core.projects_db
    with (
        factory.enable_writes(),
        pytest.raises(StorageError, match="write failed"),
        factory.rw() as connection,
    ):
        connection.execute("INSERT INTO projects VALUES ('/a', 'yesterday', 1)")
