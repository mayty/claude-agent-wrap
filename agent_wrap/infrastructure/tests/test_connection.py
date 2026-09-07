# This file has been created with the assistance of an AI tool.
"""Tests for the per-database connection factory."""

import sqlite3
from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import MigrationError, StorageError, WritesNotEnabledError
from agent_wrap.infrastructure.connection import ConnectionFactory
from agent_wrap.infrastructure.constants import DB_FILE_SUFFIX, Databases

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# As in test_migrations.py: throwaway schema, real database identity.
DATABASE_FILENAME = f"{Databases.PROJECTS}{DB_FILE_SUFFIX}"

CREATE_WIDGETS = "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL) STRICT;"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return the database file the factory fixtures below open."""
    return tmp_path / "db" / DATABASE_FILENAME


@pytest.fixture
def factory(tmp_path: Path, db_path: Path) -> ConnectionFactory:
    """
    Return a factory over a one-migration database rooted in ``tmp_path``.

    Deliberately holds no write grant -- that is the default state of a process, and the
    state the refusal tests need. Tests that write use ``writable`` below.
    """
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_create_widgets.sql").write_text(CREATE_WIDGETS, encoding="utf-8")
    return ConnectionFactory(
        name=Databases.PROJECTS,
        db_path=db_path,
        migrations_dir=migrations,
        backups_dir=tmp_path / "db" / "backups",
    )


@pytest.fixture
def writable(factory: ConnectionFactory) -> Iterator[ConnectionFactory]:
    """Yield the same factory, inside a write grant."""
    with factory.enable_writes():
        yield factory


def test_construction_creates_directory_and_migrates(
    factory: ConnectionFactory, db_path: Path
) -> None:
    assert db_path.is_file()
    with factory.ro() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1


def test_construction_enables_wal(factory: ConnectionFactory) -> None:
    """WAL is what lets the logs daemon read while a launch writes."""
    with factory.ro() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.usefixtures("factory")
def test_a_fresh_install_writes_no_backup(db_path: Path, tmp_path: Path) -> None:
    """
    Regression: the factory sets ``journal_mode`` before migrating.

    That writes the header, so by the time the first migration runs the file is no longer
    zero-length -- a size-based "is this database empty" check passes on every fresh
    install and buries the backups that matter under one useless copy per machine.
    """
    assert db_path.stat().st_size > 0
    backups = tmp_path / "db" / "backups"
    assert not backups.exists() or list(backups.iterdir()) == []


def test_a_populated_database_is_backed_up_before_a_later_migration(
    writable: ConnectionFactory, tmp_path: Path, db_path: Path
) -> None:
    """The same path as above, once there is something worth preserving."""
    with writable.rw() as connection:
        connection.execute("INSERT INTO widgets (name) VALUES ('before')")

    (tmp_path / "migrations" / "0002_add_colour.sql").write_text(
        "ALTER TABLE widgets ADD COLUMN colour TEXT;", encoding="utf-8"
    )
    ConnectionFactory(
        name=Databases.PROJECTS,
        db_path=db_path,
        migrations_dir=tmp_path / "migrations",
        backups_dir=tmp_path / "db" / "backups",
    )

    backups = list((tmp_path / "db" / "backups").iterdir())
    assert [p.name.split("-")[1] for p in backups] == ["v1"]


def test_rw_commits_on_success(writable: ConnectionFactory) -> None:
    with writable.rw() as connection:
        connection.execute("INSERT INTO widgets (name) VALUES ('kept')")

    with writable.ro() as connection:
        assert connection.execute("SELECT name FROM widgets").fetchone()["name"] == "kept"


def test_rw_rolls_back_when_the_block_raises(writable: ConnectionFactory) -> None:
    """A caller's own exception must not leave half a write behind."""

    def write_then_fail() -> None:
        with writable.rw() as connection:
            connection.execute("INSERT INTO widgets (name) VALUES ('discarded')")
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        write_then_fail()

    with writable.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM widgets").fetchone()["n"] == 0


def test_rw_wraps_sqlite_errors(writable: ConnectionFactory) -> None:
    with pytest.raises(StorageError, match="write failed"), writable.rw() as connection:
        connection.execute("INSERT INTO widgets (name) VALUES (NULL)")


def test_rw_rolls_back_on_sqlite_error(writable: ConnectionFactory) -> None:
    """The good row in a failed transaction goes with the bad one."""

    def write_good_then_bad() -> None:
        with writable.rw() as connection:
            connection.execute("INSERT INTO widgets (name) VALUES ('first')")
            connection.execute("INSERT INTO widgets (name) VALUES (NULL)")

    with pytest.raises(StorageError):
        write_good_then_bad()

    with writable.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM widgets").fetchone()["n"] == 0


def test_rw_is_refused_without_a_write_grant(factory: ConnectionFactory) -> None:
    """The default state of a process is "may not write"."""
    with pytest.raises(WritesNotEnabledError, match="writes are not enabled"), factory.rw():
        pass


def test_the_refusal_is_a_storage_error(factory: ConnectionFactory) -> None:
    """
    Load-bearing, not incidental.

    Every best-effort registry guard catches ``StorageError``; a refusal that sat outside
    that tree would crash a launch instead of costing it its registration.
    """
    with pytest.raises(StorageError), factory.rw():
        pass


def test_the_grant_does_not_outlive_its_block(factory: ConnectionFactory) -> None:
    with factory.enable_writes(), factory.rw() as connection:
        connection.execute("INSERT INTO widgets (name) VALUES ('kept')")

    with pytest.raises(WritesNotEnabledError), factory.rw():
        pass


def test_the_grant_does_not_outlive_a_raising_block(factory: ConnectionFactory) -> None:
    """A grant released by an exception must not leave writes permitted."""

    def grant_then_fail() -> None:
        with factory.enable_writes():
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        grant_then_fail()

    with pytest.raises(WritesNotEnabledError), factory.rw():
        pass


def test_enable_writes_refuses_to_nest(factory: ConnectionFactory) -> None:
    """
    One window, opened once at the top.

    A nested grant would make the permission's extent depend on the call stack, and its
    release would depend on which block unwound first.
    """
    with (
        pytest.raises(StorageError, match="must not nest"),
        factory.enable_writes(),
        factory.enable_writes(),
    ):
        pass


def test_ro_needs_no_grant(factory: ConnectionFactory) -> None:
    """Reading is always permitted -- the grant exists to gate mutation."""
    with factory.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM widgets").fetchone()["n"] == 0


def test_ro_rejects_writes(factory: ConnectionFactory) -> None:
    with pytest.raises(StorageError, match="read failed"), factory.ro() as connection:
        connection.execute("INSERT INTO widgets (name) VALUES ('nope')")


def test_ro_opens_when_no_writer_has_run(factory: ConnectionFactory, tmp_path: Path) -> None:
    """
    The logs daemon's cold start: a WAL database whose ``-shm`` file is gone.

    A ``mode=ro`` URI connection fails outright here, which is why ``ro()`` uses
    ``PRAGMA query_only`` instead.
    """
    for suffix in ("-wal", "-shm"):
        (tmp_path / "db" / f"{DATABASE_FILENAME}{suffix}").unlink(missing_ok=True)

    with factory.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM widgets").fetchone()["n"] == 0


def test_rw_propagates_non_sqlite_exceptions_unwrapped(writable: ConnectionFactory) -> None:
    """A bug in the caller's block should surface as itself, not as a StorageError."""
    with pytest.raises(ZeroDivisionError), writable.rw():
        # The literal is the point: a non-sqlite bug must surface as itself.
        _ = 1 / 0  # pyrefly: ignore [division-by-zero]


def test_construction_reports_a_broken_migration(tmp_path: Path, db_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_broken.sql").write_text("NOT SQL AT ALL;", encoding="utf-8")

    with pytest.raises(MigrationError, match=r"0001_broken\.sql"):
        ConnectionFactory(
            name=Databases.PROJECTS,
            db_path=db_path,
            migrations_dir=migrations,
            backups_dir=tmp_path / "db" / "backups",
        )


def test_connections_are_independent(writable: ConnectionFactory) -> None:
    """Each context manager opens and closes its own connection."""
    with writable.rw() as first:
        first.execute("INSERT INTO widgets (name) VALUES ('a')")
    with writable.rw() as second:
        second.execute("INSERT INTO widgets (name) VALUES ('b')")

    assert first is not second
    with pytest.raises(sqlite3.ProgrammingError):
        first.execute("SELECT 1")
