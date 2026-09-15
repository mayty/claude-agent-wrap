# This file has been created with the assistance of an AI tool.
"""The logs database's session read side: what the viewer's list endpoints ask for."""

import json
from typing import TYPE_CHECKING

from agent_wrap.infrastructure.logs.models import (
    IndexFootprint,
    Revision,
    SessionKey,
    SessionRow,
)
from agent_wrap.infrastructure.rows import row_to

if TYPE_CHECKING:
    from agent_wrap.infrastructure.connection import ConnectionFactory


class SessionRepository:
    """
    Reads the ``sessions`` table for the viewer's project and session lists.

    The counterpart to :class:`~agent_wrap.infrastructure.logs.repositories.usage.UsageRepository`,
    which is aggregate-only on purpose. This one is not, and does not need to be: a row
    here is a *session directory*, so the whole table is ~600 rows on a host with a
    2.2 GB log tree, and the viewer caches every one of them -- it renders the complete
    project and session lists. The table this must never read a row at a time from is
    ``requests``.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connections = connection_factory

    def revision(self) -> Revision:
        """
        Summarize the whole table in one probe, for change detection.

        ``MAX(last_ingested_at)`` moves whenever any session gains a record or has its
        metadata rewritten, and ``COUNT(*)`` moves when one appears or is deleted.
        Together they are what lets the viewer skip rebuilding its caches on a
        heartbeat that found nothing -- the question the old per-file ``stat()`` sweep
        of 1,834 files answered.

        Both columns come from the one statement so the pair describes a single
        snapshot; a caller reading them separately could see a count from after a
        delete and a maximum from before it.
        """
        with self._connections.ro() as connection:
            row = connection.execute(
                "SELECT MAX(last_ingested_at) AS last_ingested, COUNT(*) AS total FROM sessions"
            ).fetchone()
        return Revision(last_ingested_ns=row["last_ingested"], count=row["total"])

    def footprint(self) -> IndexFootprint:
        """
        Report how large the index is and when it was last written to.

        For a report, never for a decision -- ``agent inspect`` prints it. The size
        comes from ``page_count * page_size`` rather than from the filesystem, which
        keeps the database's path where every other path in this layer lives: in the
        argument the factory was constructed with, not in a caller that would have to
        guess the file's name.

        ``SUM(record_count)`` rather than ``COUNT(*) FROM requests``: the sessions
        table already carries the per-session totals the sessions list renders, so this
        is one narrow scan of ~600 rows and cannot disagree with what the viewer shows.
        """
        with self._connections.ro() as connection:
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
            row = connection.execute(
                "SELECT COUNT(*) AS sessions, COALESCE(SUM(record_count), 0) AS requests,"
                "       MAX(last_ingested_at) AS last_ingested"
                "  FROM sessions"
            ).fetchone()
        return IndexFootprint(
            database_bytes=pages * page_size,
            sessions=row["sessions"],
            requests=row["requests"],
            last_ingested_ns=row["last_ingested"],
        )

    def sessions(self) -> list[SessionRow]:
        """
        Return every indexed session, unfiltered and unmerged.

        Unfiltered because the caller wants all of them: it is building the viewer's
        whole project list, so a per-project query would be one statement per group and
        would still have to be repeated for every group's fingerprint. One scan of a
        narrow table answers all of it, and the grouping by ``project_hash`` -- like the
        merge across providers -- is the domain's decision to make.

        ``models`` is stored as a JSON array because it is a list the sessions list
        renders verbatim and nothing queries into; it is decoded here so no caller above
        the storage layer ever sees the encoding.
        """
        with self._connections.ro() as connection:
            rows = connection.execute(
                "SELECT project_hash, provider, claude_session_id, last_ingested_at,"
                "       record_count, last_event_at_us, models, alias, title"
                "  FROM sessions"
            ).fetchall()
        return [
            row_to(
                SessionRow,
                row,
                key=row_to(SessionKey, row),
                last_ingested_ns=row["last_ingested_at"],
                models=tuple(json.loads(row["models"])),
            )
            for row in rows
        ]
