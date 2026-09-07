# This file has been created with the assistance of an AI tool.
"""
Per-database schema migrations.

Migrations are numbered SQL scripts and nothing else: ``NNNN_<slug>.sql`` in a database
subpackage's ``migrations/`` directory, applied in order. There is deliberately no
Python escape hatch -- a migration that needs application logic (reading a legacy file,
calling a service) is not a migration, it is application code that runs on top of the
finished schema.

The applied version is ``PRAGMA user_version``, which lives in the database header and
is transactional, so a crash mid-migration leaves the previous version intact. The
backup taken before each step covers the case where it does not.
"""

import operator
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from agent_wrap.exceptions import MigrationError
from agent_wrap.infrastructure.constants import (
    BACKUP_TIMESTAMP_FORMAT,
    MIGRATION_RE,
)
from agent_wrap.infrastructure.models import MigrationFile

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.infrastructure.constants import Databases


class MigrationRunner:
    """Discovers and applies one database's ``.sql`` migrations."""

    def __init__(
        self,
        name: Databases,
        migrations_dir: Path,
        backups_dir: Path,
    ) -> None:
        self._name = name
        self._migrations_dir = migrations_dir
        self._backups_dir = backups_dir

    def discover(self) -> tuple[MigrationFile, ...]:
        """
        Return this database's migrations, ordered by version.

        Versions must run contiguously from 1. A gap means a migration was deleted or
        never committed, and a duplicate means two branches claimed the same number --
        applying either would leave ``user_version`` describing a schema the database
        does not have, so both are refused up front rather than half-applied.
        """
        if not self._migrations_dir.is_dir():
            msg = f"{self._name}: no migrations directory at {self._migrations_dir}"
            raise MigrationError(msg)

        found: list[MigrationFile] = []
        for entry in sorted(self._migrations_dir.iterdir()):
            if entry.suffix != ".sql":
                continue
            match = MIGRATION_RE.match(entry.name)
            if match is None:
                msg = f"{self._name}: migration {entry.name} is not named NNNN_<slug>.sql"
                raise MigrationError(msg)
            found.append(MigrationFile(version=int(match.group(1)), path=entry))

        found.sort(key=operator.attrgetter("version"))
        for expected, migration in enumerate(found, start=1):
            if migration.version != expected:
                msg = (
                    f"{self._name}: migration versions must run contiguously from 1 -- "
                    f"expected {expected:04d}, found {migration.path.name}"
                )
                raise MigrationError(msg)
        return tuple(found)

    def run(self, connection: sqlite3.Connection) -> None:
        """Apply every migration newer than the database's ``user_version``."""
        current = self.current_version(connection)
        for migration in self.discover():
            if migration.version <= current:
                continue
            self.back_up(connection, from_version=current)
            self.apply(connection, migration)
            current = migration.version

    def current_version(self, connection: sqlite3.Connection) -> int:
        """Return the database's applied schema version."""
        try:
            row = connection.execute("PRAGMA user_version").fetchone()
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not read user_version: {exc}"
            raise MigrationError(msg) from exc
        return int(row[0])

    def back_up(self, connection: sqlite3.Connection, *, from_version: int) -> Path | None:
        """
        Copy the database aside before a migration touches it, and return the copy.

        Returns ``None`` for a database that holds nothing yet -- a brand-new one has no
        state a migration could destroy, and writing an empty backup per fresh install
        would bury the ones that matter.

        "Holds nothing" is decided by ``sqlite_master``, which also covers the database
        that does not exist at all: opening one creates the file, and either way its
        schema catalogue is empty. It is deliberately not decided by the file's size,
        because by the time the first migration runs the file is already non-empty -- the
        factory sets ``journal_mode`` before migrating, and that writes the header. A size
        check therefore passes on every fresh install, which is exactly the case this is
        meant to skip.

        Uses SQLite's own online backup API rather than a file copy: a concurrent writer
        holding the WAL would make ``shutil.copy`` produce a torn file, whereas ``backup``
        yields a consistent snapshot.
        """
        if not self._holds_anything(connection):
            return None

        self._backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime(BACKUP_TIMESTAMP_FORMAT)
        destination = self._backups_dir / f"{self._name}-v{from_version}-{stamp}.db"
        try:
            # closing(), not a bare `with`: sqlite3's own context manager commits or
            # rolls back and leaves the connection open, which would leak a descriptor
            # per backup.
            with closing(sqlite3.connect(destination)) as target:
                connection.backup(target)
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not back up to {destination}: {exc}"
            raise MigrationError(msg) from exc
        return destination

    def _holds_anything(self, connection: sqlite3.Connection) -> bool:
        """Report whether the database defines any table, index, view or trigger."""
        try:
            row = connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not inspect the schema catalogue: {exc}"
            raise MigrationError(msg) from exc
        return bool(row[0])

    def apply(self, connection: sqlite3.Connection, migration: MigrationFile) -> None:
        """
        Run one migration script and stamp its version, as a single transaction.

        The version stamp is inside the same ``BEGIN``/``COMMIT`` as the script's own
        statements, so a failure part-way leaves both the schema and ``user_version``
        at their previous values -- there is no state in which the database claims a
        version it did not reach.
        """
        try:
            sql = migration.path.read_text(encoding="utf-8")
        except OSError as exc:
            msg = f"{self._name}: could not read {migration.path.name}: {exc}"
            raise MigrationError(msg) from exc

        script = f"BEGIN;\n{sql}\nPRAGMA user_version = {migration.version:d};\nCOMMIT;"
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            connection.rollback()
            msg = f"{self._name}: migration {migration.path.name} failed: {exc}"
            raise MigrationError(msg) from exc
