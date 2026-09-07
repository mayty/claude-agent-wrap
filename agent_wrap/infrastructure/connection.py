# This file has been created with the assistance of an AI tool.
"""
Per-database connection factory.

One instance per database file. Construction is what runs that database's migrations,
so a caller holding a factory is holding a database already at its head version -- and
because the DI container exposes each factory as a ``@cached_property``, a command that
never touches a database never opens or migrates one.

Every path is a constructor argument. Nothing here reads a module-level constant, which
is what lets a test point a whole database tree at ``tmp_path`` by constructing the DI
``Core`` rather than by monkeypatching.
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

    def __init__(
        self,
        name: Databases,
        db_path: Path,
        migrations_dir: Path,
        backups_dir: Path,
    ) -> None:
        self._name = name
        self._db_path = db_path
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

        Write permission is opt-in per database, and outside a grant :meth:`rw` refuses.
        That is what makes read-only consumers read-only structurally rather than by
        discipline: the logs daemon never opens a grant, so no read it performs can
        mutate the database however deep the call goes.

        The flag is per factory and process-wide, deliberately not thread-local -- a
        grant taken at a command's entry point has to cover the worker threads that
        command spawns (``InspectService.build_report`` probes in a thread pool). What
        keeps that comprehensible is the refusal to nest: there is exactly one window,
        opened once at the top, and a second ``enable_writes()`` is a bug rather than a
        wider permission.
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

        Refuses outright unless the caller holds a grant from :meth:`enable_writes`.

        Commits when the block completes, rolls back when it raises. Any ``sqlite3``
        failure -- including a lock the busy timeout outlived -- surfaces as
        :class:`StorageError`, so callers never have to know sqlite3's exception tree.
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

        Needs no grant from :meth:`enable_writes` -- reading is always permitted.

        Deliberately not a ``file:...?mode=ro`` URI. Under WAL a read-only open needs
        the ``-shm`` file to already exist and fails outright when it does not, which is
        exactly the logs daemon's cold start -- a second process that may open this
        database before any writer has run since boot. ``query_only`` gives the same
        "this connection cannot write" guarantee with no such failure mode.
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
            for pragma in CONNECTION_PRAGMAS:
                connection.execute(pragma)
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not open {self._db_path}: {exc}"
            raise StorageError(msg) from exc
        return connection

    def _migrate(self) -> None:
        """
        Apply the persistent PRAGMAs and every pending migration, once.

        Not gated by :meth:`enable_writes`, and does not go through :meth:`rw`. Schema
        convergence is not what the grant protects against -- unpermitted *data* writes
        are -- and gating it would leave a read-only consumer unable to open a database
        that does not exist yet, which is a crash rather than a degradation.
        """
        connection = self._connect()
        try:
            for pragma in DATABASE_PRAGMAS:
                connection.execute(pragma)
            self._migrations.run(connection)
        except sqlite3.Error as exc:
            msg = f"{self._name}: could not prepare {self._db_path}: {exc}"
            raise StorageError(msg) from exc
        finally:
            connection.close()
