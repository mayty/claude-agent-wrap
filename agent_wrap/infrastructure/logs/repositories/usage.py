# This file has been created with the assistance of an AI tool.
"""The logs database's usage read side: what `agent stats` and `usage.json` ask for."""

from typing import TYPE_CHECKING

from agent_wrap.infrastructure.logs.models import UsageCell

if TYPE_CHECKING:
    import sqlite3

    from agent_wrap.infrastructure.connection import ConnectionFactory

# The aggregate every usage window runs, at the grain a `UsageCell` describes. Public
# because the migration's plan assertion runs it -- see EXPLAIN QUERY PLAN in the logs
# migration tests, which is the only thing standing between this and a per-row table seek.
#
# Served entirely by `idx_requests_usage`, which is partial on `status = 'success'` and
# ordered by the same hour-bucket expression this groups on -- so there is no sorter and
# the table itself is never opened. `make test` asserts that plan; see the migration.
#
# `model != ''` is the "record with no model" drop, which the file scan this replaces
# performed in Python. It stays a predicate rather than joining the index's WHERE clause
# so the index remains usable by a future query that wants those rows.
#
# The bounds are hour buckets, matched against the same integer division that leads the
# index. A NULL `started_at_us` divides to NULL, so a request with no timestamp fails
# `>= :lo` and drops out of any bounded window -- which is exactly what the stats layer's
# "?" day key does with it, and why the bounds are written this way rather than as an
# OR-ed IS NULL.
USAGE_CELLS = """
    SELECT started_at_us / 3600000000 AS hour_bucket,
           session_id,
           model,
           usage_source,
           COUNT(*)                   AS requests,
           MAX(started_at_us)         AS last_started_at_us,
           SUM(input_tokens)          AS input_tokens,
           SUM(output_tokens)         AS output_tokens,
           SUM(cache_write_tokens)    AS cache_write_tokens,
           SUM(cache_write_5m)        AS cache_write_5m,
           SUM(cache_write_1h)        AS cache_write_1h,
           SUM(cache_read)            AS cache_read
      FROM requests
     WHERE status = 'success'
       AND model != ''
       AND (:lo IS NULL OR started_at_us / 3600000000 >= :lo)
       AND (:hi IS NULL OR started_at_us / 3600000000 <= :hi)
  GROUP BY hour_bucket, session_id, model, usage_source
"""


class UsageRepository:
    """
    Reads token usage out of the ingested index, aggregated and already summed.

    Deliberately narrow: one question, answered by an aggregate rather than by anything
    a caller can page through. This is the repository that replaced a 2.2 GB re-parse of
    the log tree, and keeping it aggregate-only is what stops a consumer from
    reintroducing one row at a time.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connections = connection_factory

    def usage_cells(
        self, *, from_hour: int | None = None, until_hour: int | None = None
    ) -> list[UsageCell]:
        """
        Return every in-window usage cell, each tagged with its project's hash.

        The bounds are inclusive hour buckets (hours since the epoch), or ``None`` for
        an open side. They are applied in SQL rather than after the fact because the
        index's leading term is that bucket, so a bounded window is a range scan over
        exactly the rows it covers.

        The project hash and provider come from a second statement, not a join. Joining
        ``sessions`` would make the planner resolve a parent row per *request* -- 44k
        rowid seeks to label 1.8k groups -- and would cost the aggregate its covering
        plan. One ~600-row scan of ``sessions`` labels every cell instead, and both
        statements run on the same connection so they see one snapshot.
        """
        with self._connections.ro() as connection:
            rows = connection.execute(USAGE_CELLS, {"lo": from_hour, "hi": until_hour}).fetchall()
            owners = self._session_owners(connection)
        return [
            UsageCell(
                hour_bucket=row["hour_bucket"],
                # A cell whose session row vanished between the two statements cannot
                # happen inside one read transaction, so the fallback is unreachable
                # rather than a real case -- but it is empty strings, not a crash,
                # because a missing hash would only mean the spend renders as orphaned.
                project_hash=owners.get(row["session_id"], ("", ""))[0],
                provider=owners.get(row["session_id"], ("", ""))[1],
                session_id=row["session_id"],
                model=row["model"],
                usage_source=row["usage_source"],
                requests=row["requests"],
                last_started_at_us=row["last_started_at_us"],
                input_tokens=row["input_tokens"],
                output_tokens=row["output_tokens"],
                cache_write_tokens=row["cache_write_tokens"],
                cache_write_5m=row["cache_write_5m"],
                cache_write_1h=row["cache_write_1h"],
                cache_read=row["cache_read"],
            )
            for row in rows
        ]

    @staticmethod
    def _session_owners(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
        return {
            row["id"]: (row["project_hash"], row["provider"])
            for row in connection.execute("SELECT id, project_hash, provider FROM sessions")
        }
