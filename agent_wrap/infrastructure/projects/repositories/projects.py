# This file has been created with the assistance of an AI tool.
"""The project registry repository."""

import time
from typing import TYPE_CHECKING

from agent_wrap.infrastructure.projects.models import Project, Revision

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

    from agent_wrap.infrastructure.connection import ConnectionFactory


class ProjectsRepository:
    """
    Reads and writes registered project directories.

    Translates rows to :class:`Project` and back. Nothing above this class sees a
    ``sqlite3`` type, a column name, or SQL -- which is the point: the storage layer can
    be reshaped without any other layer changing.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connections = connection_factory

    def list_projects(self) -> list[Project]:
        """
        Return every registered project, ordered by path.

        The ordering is load-bearing rather than cosmetic: the logs viewer derives group
        ids from this sequence's positions, so a project's id would shift under it if the
        order were left to the database.
        """
        with self._connections.ro() as connection:
            rows = connection.execute(
                "SELECT path, first_seen_at, last_seen_at FROM projects ORDER BY path"
            ).fetchall()
        return [
            Project(
                path=row["path"],
                first_seen_at=row["first_seen_at"],
                last_seen_at=row["last_seen_at"],
            )
            for row in rows
        ]

    def record(self, path: str) -> None:
        """Register *path*, or refresh it if it is already registered."""
        with self._connections.rw() as connection:
            self._upsert(connection, (path,))

    def record_many(self, paths: Sequence[str]) -> None:
        """
        Register every path in *paths* in a single transaction.

        All-or-nothing, and one clock reading for the batch, so a bulk registration
        cannot land half-done and every row it writes shares a ``last_seen_at``. Empty
        input opens no transaction at all.
        """
        if not paths:
            return
        with self._connections.rw() as connection:
            self._upsert(connection, paths)

    @staticmethod
    def _upsert(connection: sqlite3.Connection, paths: Sequence[str]) -> None:
        """
        Write *paths* through the registry's upsert on an open transaction.

        An upsert rather than an insert, so concurrent launches from the same directory
        cannot collide and a re-registration keeps the original ``first_seen_at``.
        ``last_seen_at`` moves on every call even when nothing else changed -- that is
        what the logs viewer's ETag reads, and it mirrors the registry file's mtime
        moving on every launch.
        """
        now = time.time_ns()
        for path in paths:
            connection.execute(
                "INSERT INTO projects (path, first_seen_at, last_seen_at) VALUES (?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET last_seen_at = excluded.last_seen_at",
                (path, now, now),
            )

    def delete(self, paths: Sequence[str]) -> None:
        """Unregister *paths*. Absent paths are ignored."""
        if not paths:
            return
        with self._connections.rw() as connection:
            connection.executemany("DELETE FROM projects WHERE path = ?", [(p,) for p in paths])

    def revision(self) -> Revision:
        """
        Return a cheap summary that changes whenever the table does.

        Costs one aggregate query -- the same order of work as the ``stat()`` on the
        registry file it replaces.
        """
        with self._connections.ro() as connection:
            row = connection.execute(
                "SELECT MAX(last_seen_at) AS last_change, COUNT(*) AS total FROM projects"
            ).fetchone()
        return Revision(last_change_ns=row["last_change"], count=row["total"])
