# This file has been created with the assistance of an AI tool.
"""Tests for the migration runner, driven against throwaway migration scripts."""

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from agent_wrap.exceptions import MigrationError
from agent_wrap.infrastructure.constants import DB_FILE_SUFFIX, Databases
from agent_wrap.infrastructure.migrations import MigrationRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

# The runner is driven with throwaway scripts, so the schema below is invented -- but the
# database's *identity* is a real ``Databases`` member, since that is the only thing the
# runner accepts and it is what backup filenames are built from.
DATABASE_FILENAME = f"{Databases.PROJECTS}{DB_FILE_SUFFIX}"
BACKUP_PREFIX = f"{Databases.PROJECTS}-v"

CREATE_WIDGETS = "CREATE TABLE widgets (id INTEGER PRIMARY KEY, name TEXT NOT NULL) STRICT;"
ADD_COLOUR = "ALTER TABLE widgets ADD COLUMN colour TEXT;"


@pytest.fixture
def write_migration(tmp_path: Path) -> Callable[[str, str], Path]:
    """Return a factory writing a named migration script into the migrations dir."""

    def _write(filename: str, sql: str) -> Path:
        migrations = tmp_path / "migrations"
        migrations.mkdir(parents=True, exist_ok=True)
        path = migrations / filename
        path.write_text(sql, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def runner(tmp_path: Path) -> MigrationRunner:
    """Return a runner over ``tmp_path``, whose migrations dir the test populates."""
    return MigrationRunner(
        name=Databases.PROJECTS,
        migrations_dir=tmp_path / "migrations",
        backups_dir=tmp_path / "backups",
    )


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield an autocommit connection to the database the runner is driven against."""
    with closing(sqlite3.connect(tmp_path / DATABASE_FILENAME, isolation_level=None)) as conn:
        yield conn


def test_run_applies_pending_migrations(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
) -> None:
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    write_migration("0002_add_colour.sql", ADD_COLOUR)

    runner.run(connection)

    assert runner.current_version(connection) == 2
    columns = {row[1] for row in connection.execute("PRAGMA table_info(widgets)")}
    assert columns == {"id", "name", "colour"}


def test_run_is_idempotent(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
) -> None:
    """A second run applies nothing, so a re-run cannot fail on an existing table."""
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    runner.run(connection)

    runner.run(connection)

    assert runner.current_version(connection) == 1


def test_run_applies_only_new_migrations(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
) -> None:
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    runner.run(connection)
    connection.execute("INSERT INTO widgets (name) VALUES ('kept')")

    write_migration("0002_add_colour.sql", ADD_COLOUR)
    runner.run(connection)

    assert runner.current_version(connection) == 2
    assert connection.execute("SELECT name FROM widgets").fetchone()[0] == "kept"


def test_run_skips_backup_for_fresh_database(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
    tmp_path: Path,
) -> None:
    """A database with nothing in it has no state worth preserving."""
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)

    runner.run(connection)

    assert not (tmp_path / "backups").exists()


def test_run_skips_backup_for_a_fresh_database_with_a_written_header(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
    tmp_path: Path,
) -> None:
    """
    Emptiness is about content, not file size.

    ``ConnectionFactory`` sets ``journal_mode`` before migrating, which writes the header
    and makes even a schema-less database non-empty on disk. Judging by size here would
    write one useless backup per fresh install.
    """
    connection.execute("PRAGMA journal_mode = WAL")
    assert (tmp_path / DATABASE_FILENAME).stat().st_size > 0
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)

    runner.run(connection)

    assert not (tmp_path / "backups").exists()


def test_run_backs_up_before_migrating_populated_database(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
    tmp_path: Path,
) -> None:
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    runner.run(connection)
    connection.execute("INSERT INTO widgets (name) VALUES ('before')")

    write_migration("0002_add_colour.sql", ADD_COLOUR)
    runner.run(connection)

    backups = list((tmp_path / "backups").iterdir())
    assert len(backups) == 1
    assert backups[0].name.startswith(f"{BACKUP_PREFIX}1-")
    with closing(sqlite3.connect(backups[0])) as restored:
        assert restored.execute("SELECT name FROM widgets").fetchone()[0] == "before"
        assert "colour" not in {row[1] for row in restored.execute("PRAGMA table_info(widgets)")}


def test_backups_accumulate(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
    tmp_path: Path,
) -> None:
    """Backups are never pruned -- one per applied step on a populated database."""
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    runner.run(connection)
    connection.execute("INSERT INTO widgets (name) VALUES ('seed')")

    write_migration("0002_add_colour.sql", ADD_COLOUR)
    write_migration("0003_add_size.sql", "ALTER TABLE widgets ADD COLUMN size INTEGER;")
    runner.run(connection)

    versions = sorted(p.name.split("-")[1] for p in (tmp_path / "backups").iterdir())
    assert versions == ["v1", "v2"]


def test_failing_migration_leaves_version_untouched(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
) -> None:
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    runner.run(connection)
    write_migration("0002_broken.sql", "ALTER TABLE widgets ADD COLUMN name TEXT;")

    with pytest.raises(MigrationError, match=r"0002_broken\.sql"):
        runner.run(connection)

    assert runner.current_version(connection) == 1


def test_failing_migration_rolls_back_its_own_statements(
    runner: MigrationRunner,
    connection: sqlite3.Connection,
    write_migration: Callable[[str, str], Path],
) -> None:
    """The version stamp shares a transaction with the script, so neither half survives."""
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    write_migration(
        "0002_partly_broken.sql",
        "CREATE TABLE gadgets (id INTEGER PRIMARY KEY) STRICT;\nSELECT nonexistent();",
    )

    with pytest.raises(MigrationError):
        runner.run(connection)

    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
    assert "gadgets" not in tables


@pytest.mark.parametrize(
    ("filenames", "expected"),
    [
        (("0002_only.sql",), "expected 0001"),
        (("0001_a.sql", "0003_c.sql"), "expected 0002"),
        (("0001_a.sql", "0001_b.sql"), "contiguously"),
    ],
)
def test_discover_rejects_non_contiguous_versions(
    runner: MigrationRunner,
    write_migration: Callable[[str, str], Path],
    filenames: tuple[str, ...],
    expected: str,
) -> None:
    for filename in filenames:
        write_migration(filename, CREATE_WIDGETS)

    with pytest.raises(MigrationError, match=expected):
        runner.discover()


def test_discover_rejects_misnamed_script(
    runner: MigrationRunner, write_migration: Callable[[str, str], Path]
) -> None:
    write_migration("create_widgets.sql", CREATE_WIDGETS)

    with pytest.raises(MigrationError, match=r"NNNN_<slug>\.sql"):
        runner.discover()


def test_discover_ignores_non_sql_files(
    runner: MigrationRunner, write_migration: Callable[[str, str], Path]
) -> None:
    """A stray README beside the scripts is not a malformed migration."""
    write_migration("0001_create_widgets.sql", CREATE_WIDGETS)
    write_migration("README.md", "notes")

    assert [m.version for m in runner.discover()] == [1]


def test_discover_rejects_missing_directory(tmp_path: Path) -> None:
    runner = MigrationRunner(
        name=Databases.PROJECTS,
        migrations_dir=tmp_path / "absent",
        backups_dir=tmp_path / "backups",
    )

    with pytest.raises(MigrationError, match="no migrations directory"):
        runner.discover()
