# This file has been created with the assistance of an AI tool.
"""
Per-database connection factory.

Construction runs that database's migrations, so a caller holding a factory holds a
database already at its head version -- and since the DI container exposes each as a
``@cached_property``, a command that touches no database opens and migrates none.

Every path and PRAGMA set is a constructor argument, never a module-level constant, which
is what lets a test point a whole database tree at ``tmp_path`` by constructing the DI
``Core``. A database whose storage profile differs states its own set at the composition
root: the logs blob store wants a larger page size and incremental auto-vacuum, neither
of which can be applied after its first page exists.
"""

import sqlite3
from contextlib import contextmanager
from typing import TYPE_CHECKING

from agent_wrap.exceptions import StorageError, WritesNotEnabledError
from agent_wrap.infrastructure.constants import CONNECTION_PRAGMAS, DATABASE_PRAGMAS
from agent_wrap.infrastructure.migrations import MigrationRunner

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from agent_wrap.infrastructure.constants import Databases


class ConnectionFactory:
    """Opens connections to one SQLite database, and migrates it on construction."""

    def __init__(  # noqa: PLR0913 -- six keyword-only settings, not a positional signature
        self,
        *,
        name: Databases,
        db_path: Path,
        migrations_dir: Path,
        backups_dir: Path,
        database_pragmas: tuple[str, ...] = DATABASE_PRAGMAS,
        connection_pragmas: tuple[str, ...] = CONNECTION_PRAGMAS,
    ) -> None:
        self._name = name
        self._db_path = db_path
        # PRAGMAs are arguments for the same reason paths are (see the module docstring):
        # a database with different storage characteristics -- the logs blob store above
        # all -- states its own, and the composition root decides which set each gets.
        # The defaults keep every existing caller unchanged.
        self._database_pragmas = database_pragmas
        self._connection_pragmas = connection_pragmas
        self._migrations = MigrationRunner(
            name=name,
            migrations_dir=migrations_dir,
            backups_dir=backups_dir,
        )
        self._writes_enabled = False
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    @contextmanager
    def enable_writes(self) -> Generator[None]:
        """
        Permit :meth:`rw` for the duration of the block.

        Outside a grant :meth:`rw` refuses, which makes read-only consumers read-only
        structurally rather than by discipline: the logs daemon never opens a grant.

        Process-wide rather than thread-local, because a grant taken at a command's entry
        point must cover the worker threads it spawns. Nesting is refused, so there is
        exactly one window and a second ``enable_writes()`` is a bug.
        """
        if self._writes_enabled:
            msg = f"{self._name}: enable_writes() must not nest"
            raise StorageError(msg)
        self._writes_enabled = True
        try:
            yield
        finally:
            self._writes_enabled = False

    @contextmanager
    def rw(self) -> Generator[sqlite3.Connection]:
        """
        Yield a read-write connection wrapping the block in one transaction.

        Refuses unless the caller holds a grant from :meth:`enable_writes`. Any
        ``sqlite3`` failure surfaces as :class:`StorageError`.
        """
        if not self._writes_enabled:
            msg = f"{self._name}: writes are not enabled in this process"
            raise WritesNotEnabledError(msg)
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            yield connection
        except sqlite3.Error as exc:
            connection.rollback()
            msg = f"{self._name}: write failed: {exc}"
            raise StorageError(msg) from exc
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def ro(self) -> Generator[sqlite3.Connection]:
        """
        Yield a connection that cannot write, via ``PRAGMA query_only``.

        Deliberately not a ``file:...?mode=ro`` URI: under WAL that needs the ``-shm``
        file to already exist and fails outright when it does not, which is exactly the
        logs daemon's cold start. ``query_only`` gives the same guarantee with no such
        failure mode.
        """
        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only = ON")
            yield connection
        except sqlite3.Error as exc:
            msg = f"{self._name}: read failed: {exc}"
            raise StorageError(msg) from exc
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        """Open a connection in autocommit mode with the per-connection PRAGMAs applied."""
        try:
            # isolation_level=None: sqlite3 issues no implicit BEGIN, so transaction
            # boundaries are the explicit ones rw() and the migration scripts state.
            connection = sqlite3.connect(self._db_path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            for pragma in self._connection_pragmas:
                connection.execute(pragma)
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not open {self._db_path}: {exc}"
            raise StorageError(msg) from exc
        return connection

    def _migrate(self) -> None:
        """
        Apply the persistent PRAGMAs and every pending migration, once.

        Deliberately not gated by :meth:`enable_writes`: the grant protects against
        unpermitted *data* writes, and gating schema convergence would leave a read-only
        consumer unable to open a database that does not exist yet.
        """
        connection = self._connect()
        try:
            for pragma in self._database_pragmas:
                connection.execute(pragma)
            self._migrations.run(connection)
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not prepare {self._db_path}: {exc}"
            raise StorageError(msg) from exc
        finally:
            connection.close()
