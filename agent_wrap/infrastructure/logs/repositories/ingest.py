# This file has been created with the assistance of an AI tool.
"""The logs database's write side: every insert, upsert and delete lives here."""

import array
import json
import time
import zlib
from typing import TYPE_CHECKING

from agent_wrap.constants import HASH_POINTER_PREFIX, HASH_POINTER_RE
from agent_wrap.infrastructure.logs.constants import (
    BLOB_ID_TYPECODE,
    CODEC_RAW,
    CODEC_ZLIB,
    MAX_QUERY_PARAMETERS,
    ZLIB_LEVEL,
    ZLIB_MIN_BYTES,
)
from agent_wrap.infrastructure.logs.models import (
    BlobSweep,
    SessionKey,
    SessionState,
    SessionSummary,
    SessionWatermark,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Iterator, Sequence

    from agent_wrap.infrastructure.connection import ConnectionFactory
    from agent_wrap.infrastructure.logs.models import IngestChunk

# `array` is native-endian, so a big-endian host must swap to keep the stored vector
# little-endian and the database file portable between machines. Computed once.
NEEDS_BYTESWAP = array.array(BLOB_ID_TYPECODE, [1]).tobytes() != b"\x01\x00\x00\x00"


class BlobCodec:
    """
    How a blob's bytes and a request's reference vector are stored, and read back.

    A namespace class rather than loose functions: these four are one micro-domain --
    the encoding boundary between an app value and a column -- and both the ingest
    writer and the read repositories need them.
    """

    @staticmethod
    def encode(raw: bytes) -> tuple[str, bytes]:
        """
        Return ``(codec, payload)`` for *raw*, compressing when it is worth it.

        The codec is stored explicitly rather than sniffed from the payload, because a
        raw payload may legitimately begin with zlib's 0x78 magic and there would be no
        way to tell the two apart.
        """
        if len(raw) < ZLIB_MIN_BYTES:
            return CODEC_RAW, raw
        return CODEC_ZLIB, zlib.compress(raw, ZLIB_LEVEL)

    @staticmethod
    def decode(codec: str, payload: bytes) -> bytes:
        """Invert :meth:`encode`."""
        return zlib.decompress(payload) if codec == CODEC_ZLIB else payload

    @staticmethod
    def pack_ids(blob_ids: Iterable[int]) -> bytes:
        """
        Pack blob ids into a little-endian uint32 vector, in message order.

        Not a junction table: 3.2M message references would be ~75 MB of rows plus a
        mandatory index on ``blob_id``, without which ``foreign_keys = ON`` turns every
        blob delete into a full scan of three million rows. Packed, the same data is
        ~12 MB, and the set difference between two vectors is what makes the viewer's
        incremental tick a few kilobytes.
        """
        packed = array.array(BLOB_ID_TYPECODE, blob_ids)
        if NEEDS_BYTESWAP:  # pragma: no cover -- no big-endian host in CI
            packed.byteswap()
        return packed.tobytes()

    @staticmethod
    def unpack_ids(payload: bytes) -> tuple[int, ...]:
        """Invert :meth:`pack_ids`."""
        unpacked = array.array(BLOB_ID_TYPECODE)
        unpacked.frombytes(payload)
        if NEEDS_BYTESWAP:  # pragma: no cover -- no big-endian host in CI
            unpacked.byteswap()
        return tuple(unpacked)


class LogIngestRepository:
    """
    Writes the ingested index. Pure SQL plus the blob codec -- no parsing.

    The split with ``domain/logs/ingest.py`` is deliberate: that module knows the log
    file format, this one knows the schema, and neither knows the other's subject. What
    crosses between them is :class:`IngestChunk`, a value type carrying no behaviour.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connections = connection_factory

    def session_state(self, key: SessionKey) -> SessionState | None:
        """
        Return where ingest left off for *key*, or ``None`` if it has never been seen.

        ``None`` and a zero state mean the same thing to a caller about to ingest, but
        differ to one asking whether a directory has ever been looked at, so the
        distinction is preserved rather than collapsed into ``SessionState.unseen()``.
        """
        with self._connections.ro() as connection:
            row = connection.execute(
                "SELECT messages_offset, strings_offset, record_count, last_event_at_us,"
                "       models, alias, title"
                "  FROM sessions"
                " WHERE project_hash = ? AND provider = ? AND claude_session_id = ?",
                tuple(key),
            ).fetchone()
        if row is None:
            return None
        return SessionState(
            messages_offset=row["messages_offset"],
            strings_offset=row["strings_offset"],
            summary=SessionSummary(
                record_count=row["record_count"],
                last_event_at_us=row["last_event_at_us"],
                models=tuple(json.loads(row["models"])),
                alias=row["alias"],
                title=row["title"],
            ),
        )

    def watermarks(self) -> list[SessionWatermark]:
        """
        Return every indexed session and how far into its messages file ingest has read.

        A read on the write-side repository because a watermark *is* ingest state, and
        this is where the other reader of it (:meth:`session_state`) already lives. The
        whole table, unfiltered: the caller compares each offset against the live file
        size to see how far behind the index has fallen, and a session missing from this
        list is one the index has never seen at all -- which the caller detects by
        walking the tree, not by asking here.
        """
        with self._connections.ro() as connection:
            rows = connection.execute(
                "SELECT project_hash, provider, claude_session_id, messages_offset  FROM sessions"
            ).fetchall()
        return [
            SessionWatermark(
                key=SessionKey(
                    project_hash=row["project_hash"],
                    provider=row["provider"],
                    claude_session_id=row["claude_session_id"],
                ),
                messages_offset=row["messages_offset"],
            )
            for row in rows
        ]

    def reset_session(self, key: SessionKey) -> None:
        """
        Forget *key* entirely, cascading its requests, so it can be ingested from zero.

        Truncation recovery. A source file smaller than its own watermark was replaced
        rather than appended to, which makes every stored offset meaningless: there is
        no way to know which existing rows still correspond to file content, so none are
        kept.

        Orphaned blobs are deliberately left behind. They are content-addressed, so the
        re-ingest re-uses them rather than duplicating them, and reclaiming them is the
        blob sweep's job.
        """
        with self._connections.rw() as connection:
            connection.execute(
                "DELETE FROM sessions"
                " WHERE project_hash = ? AND provider = ? AND claude_session_id = ?",
                tuple(key),
            )

    def ingest_chunk(self, key: SessionKey, chunk: IngestChunk) -> None:
        """
        Apply one chunk: blobs, requests, the session summary, and both watermarks.

        All in one transaction, so an interrupted ingest leaves the watermarks exactly
        where the last complete chunk left them and the next pass resumes there. No
        statement here is idempotent by itself -- it is the offsets that make a re-run
        safe, by never presenting the same lines twice.
        """
        with self._connections.rw() as connection:
            session_id = self._upsert_session(connection, key, chunk)
            blob_ids = self._insert_blobs(connection, chunk)
            self._insert_requests(connection, session_id, chunk, blob_ids)

    def delete_projects(self, project_hashes: Sequence[str]) -> None:
        """Drop every session belonging to *project_hashes*, cascading their requests."""
        if not project_hashes:
            return
        with self._connections.rw() as connection:
            connection.executemany(
                "DELETE FROM sessions WHERE project_hash = ?",
                [(project_hash,) for project_hash in project_hashes],
            )

    def expired_sessions(self, cutoff_us: int) -> list[SessionWatermark]:
        """
        Return every session whose newest request is older than *cutoff_us*.

        Dated by ``last_event_at_us`` -- the last record's own end instant -- and never
        by ``last_ingested_at``, which says when *this host* read the session rather
        than when it happened. A backfill stamps the whole tree with today, so dating a
        session by it would make retention unable to expire anything until the age had
        elapsed again since the backfill.

        A session with no ``last_event_at_us`` at all is never returned. Every one of
        its records lacked a timing, so there is no instant to compare, and the caller
        deletes files: guessing an age for content that cannot be dated is the one
        mistake here that cannot be undone.

        The watermark comes back with the key because the caller needs it. Age is not
        sufficient grounds to delete a session -- the caller also has to know that the
        index has read the whole of its file, which is a comparison against the live
        size and therefore not a question this layer can ask.
        """
        with self._connections.ro() as connection:
            rows = connection.execute(
                "SELECT project_hash, provider, claude_session_id, messages_offset"
                "  FROM sessions"
                " WHERE last_event_at_us IS NOT NULL AND last_event_at_us < ?",
                (cutoff_us,),
            ).fetchall()
        return [
            SessionWatermark(
                key=SessionKey(
                    project_hash=row["project_hash"],
                    provider=row["provider"],
                    claude_session_id=row["claude_session_id"],
                ),
                messages_offset=row["messages_offset"],
            )
            for row in rows
        ]

    def delete_sessions(self, keys: Sequence[SessionKey]) -> None:
        """
        Drop each named session, cascading its requests.

        Per session rather than per project, which is what separates retention from
        :meth:`delete_projects`: a project's logs are deleted whole because the project
        itself is gone, while retention takes the old sessions out of a project that is
        still very much alive.

        Orphaned blobs are left behind exactly as :meth:`reset_session` leaves them --
        reclaiming them is :meth:`sweep_blobs`' job, and the caller runs it next.
        """
        if not keys:
            return
        with self._connections.rw() as connection:
            connection.executemany(
                "DELETE FROM sessions"
                " WHERE project_hash = ? AND provider = ? AND claude_session_id = ?",
                [tuple(key) for key in keys],
            )

    def sweep_blobs(self) -> BlobSweep:
        """
        Delete every blob nothing reaches any more, and return the pages to the file.

        Reachability, not reference counting. There is no refcount column and there
        deliberately is not one: ``message_refs`` is a packed vector with no foreign
        key, so a count would have to be maintained by hand on every insert and every
        cascade, and one missed decrement is either a leak forever or -- far worse --
        content deleted while a request still points at it. A sweep asks the question
        from scratch instead, which cannot drift.

        Three kinds of reference reach a blob, and all three are followed here:

        * **structural** -- a request's ``message_refs`` vector and its three blob
          columns. These are ids, and reading them is what the first pass does.
        * **textual, in a payload** -- a ``hash:<sha256>`` pointer *inside another
          blob's bytes*. The sidecar interns every string of 70 characters or more, so
          most of a conversation's actual text is reached only this way. Nothing in SQL
          can see it: the payloads are zlib and opaque, so the closure is computed by
          decoding each reachable blob and scanning its text.
        * **textual, in a column** -- the same pointer in ``requests.error``, which
          stores one verbatim rather than as a blob reference. Missing this one would
          delete the message of every failure long enough to have been interned.

        The textual kinds are why this runs under ``agent cleanup`` and never on a
        timer. They are the only thing standing between a bug and deleted content, they
        cost a decode of every reachable blob, and that is worth paying for exactly
        when the user has asked to reclaim space.

        The scan is transitive because it has to be: an interned original is itself
        arbitrary text and may quote a pointer, so each newly reached blob joins the
        next frontier. It terminates because every round either empties the frontier or
        removes at least one candidate from the set.

        Measured on a 2.2 GB log tree: 334k blobs, of which 172k are named by a request
        and 162k are interned strings reached only by a pointer. The whole pass takes
        about five seconds, nearly all of it in the closure. It found 435 unreachable
        blobs (0.15 MB) on an index with nothing deleted from it, and they are genuine:
        ingest interns every string in ``strings.jsonl``, including the ones quoted only
        by the parts of a record it deliberately does not store -- the request headers
        and litellm's own ``request.body.metadata``. No stored record can ever resolve
        those, so reclaiming them is the point rather than a surprise.

        **The caller must hold the ingest lock.** The read pass and the delete are two
        transactions, and a writer landing between them could commit a request
        referencing a blob this pass judged unreachable -- content-addressed dedup lets
        a chunk reference a blob it does not carry, so the row it needs would already
        be gone. Every writer of this database takes that lock; a sweep is a writer.
        """
        with self._connections.ro() as connection:
            reachable = self._referenced_blob_ids(connection)
            candidates = self._unreferenced_blobs(connection, reachable)
            self._follow_pointers(connection, reachable, candidates)
        doomed = sorted(candidates.values())
        if not doomed:
            return BlobSweep(removed=0, freed_bytes=0)
        with self._connections.rw() as connection:
            freed = self._delete_blobs(connection, doomed)
        return BlobSweep(removed=len(doomed), freed_bytes=freed)

    @staticmethod
    def _referenced_blob_ids(connection: sqlite3.Connection) -> set[int]:
        """
        Every blob id a request row names, from all four of its reference columns.

        The one place besides ``RequestRepository`` that reads ``requests`` a row at a
        time, and it stays inside the layer: nothing here is returned, only the set of
        ids it implies. There is no aggregate that could replace it, because the
        references are inside a packed vector SQL cannot open -- see rule 3 in
        ``docs/infrastructure.md``.

        Streamed off the cursor rather than fetched, so the whole table's vectors are
        never resident at once.
        """
        found: set[int] = set()
        rows = connection.execute(
            "SELECT message_refs, system_blob, tools_blob, response_blob FROM requests"
        )
        for row in rows:
            found.update(BlobCodec.unpack_ids(row["message_refs"]))
            found.update(
                blob_id
                for blob_id in (row["system_blob"], row["tools_blob"], row["response_blob"])
                if blob_id is not None
            )
        return found

    @staticmethod
    def _unreferenced_blobs(
        connection: sqlite3.Connection, reachable: set[int]
    ) -> dict[bytes, int]:
        """
        Return ``address -> id`` for every blob no request names, as sweep candidates.

        Keyed by address because that is what a ``hash:`` pointer resolves to, and the
        pointer scan is the only thing that can still save one of these. Most of them
        *will* be saved: an interned string is never named by a request, so on a healthy
        index this set is roughly every string in it.
        """
        return {
            row["sha256"]: row["id"]
            for row in connection.execute("SELECT id, sha256 FROM blobs")
            if row["id"] not in reachable
        }

    @classmethod
    def _follow_pointers(
        cls,
        connection: sqlite3.Connection,
        reachable: set[int],
        candidates: dict[bytes, int],
    ) -> None:
        """
        Remove from *candidates* every blob a pointer in reachable text points at.

        Mutates *candidates* down to the doomed set, so what remains when this returns
        is exactly what nothing reaches. The first round scans both textual roots -- the
        error column and the payloads of every structurally referenced blob -- and each
        round after it scans only what the previous round reached, which is what keeps a
        transitive closure from re-reading the whole store per round.
        """
        frontier = cls._claim(cls._error_texts(connection), candidates)
        frontier += cls._claim(cls._blob_texts(connection, sorted(reachable)), candidates)
        while frontier and candidates:
            frontier = cls._claim(cls._blob_texts(connection, sorted(frontier)), candidates)

    @staticmethod
    def _claim(texts: Iterator[str], candidates: dict[bytes, int]) -> list[int]:
        """
        Take out of *candidates* every blob the pointers in *texts* name, and return them.

        A pointer's 64 hex characters *are* the content address, so no lookup is needed
        to resolve one -- membership of the candidate dictionary answers it. Removing as
        it goes is what makes the loop above terminate, and what stops a blob two
        different texts point at being scanned twice.
        """
        reached: list[int] = []
        for text in texts:
            for pointer in HASH_POINTER_RE.findall(text):
                address = bytes.fromhex(pointer.removeprefix(HASH_POINTER_PREFIX))
                blob_id = candidates.pop(address, None)
                if blob_id is not None:
                    reached.append(blob_id)
        return reached

    @staticmethod
    def _error_texts(connection: sqlite3.Connection) -> Iterator[str]:
        """
        Yield every request's error message, which may itself be a pointer.

        Not filtered to ``LIKE 'hash:%'``: the callback hashes an exception message
        whole, so a pointer here is the entire column today -- but a message that merely
        *contains* one costs the same regex either way, and the failure rows are a small
        fraction of the table.
        """
        for row in connection.execute("SELECT error FROM requests WHERE error IS NOT NULL"):
            yield row["error"]

    @classmethod
    def _blob_texts(cls, connection: sqlite3.Connection, blob_ids: Sequence[int]) -> Iterator[str]:
        """
        Yield the decoded text of each named blob, one row at a time.

        Deliberately a generator over the cursor: the caller scans as it goes, and
        materializing a batch would put hundreds of megabytes of decompressed
        conversation in memory to answer a question about ids.

        Decoded with ``errors="replace"`` for the same reason the read side is -- ingest
        encodes with ``surrogatepass``, so a payload is not always valid UTF-8. A
        replacement character cannot invent or destroy a pointer, which is all this
        text is scanned for.
        """
        for batch in cls._batched(blob_ids):
            placeholders = ", ".join("?" * len(batch))
            rows = connection.execute(
                f"SELECT codec, payload FROM blobs WHERE id IN ({placeholders})",  # noqa: S608
                batch,
            )
            for row in rows:
                yield BlobCodec.decode(row["codec"], row["payload"]).decode(
                    "utf-8", errors="replace"
                )

    @classmethod
    def _delete_blobs(cls, connection: sqlite3.Connection, blob_ids: Sequence[int]) -> int:
        """
        Delete the named blobs and reclaim their pages, returning the payload bytes.

        The size is measured before the delete and in the same transaction, so the
        figure reported is the content that actually went.

        ``incremental_vacuum`` is what makes the file shrink rather than merely gain a
        free list -- the reason ``auto_vacuum = INCREMENTAL`` is set when the database
        is created. It has to be *stepped*: the pragma is a query, and executing it
        without draining the result frees exactly one page.
        """
        freed = 0
        for batch in cls._batched(blob_ids):
            placeholders = ", ".join("?" * len(batch))
            row = connection.execute(
                "SELECT COALESCE(SUM(length(payload)), 0) AS bytes"  # noqa: S608
                f"  FROM blobs WHERE id IN ({placeholders})",
                batch,
            ).fetchone()
            freed += row["bytes"]
            connection.execute(
                f"DELETE FROM blobs WHERE id IN ({placeholders})",  # noqa: S608
                batch,
            )
        connection.execute("PRAGMA incremental_vacuum").fetchall()
        return freed

    @staticmethod
    def _upsert_session(connection: sqlite3.Connection, key: SessionKey, chunk: IngestChunk) -> int:
        """
        Create or refresh the session row and return its id.

        An upsert because a session is ingested repeatedly as it grows. The summary
        columns are assigned rather than accumulated -- the parser reports cumulative
        totals, so replaying a chunk cannot inflate them.
        """
        summary = chunk.summary
        row = connection.execute(
            "INSERT INTO sessions ("
            "  project_hash, provider, claude_session_id,"
            "  messages_offset, strings_offset, last_ingested_at,"
            "  record_count, last_event_at_us, models, alias, title"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (project_hash, provider, claude_session_id) DO UPDATE SET"
            "  messages_offset  = excluded.messages_offset,"
            "  strings_offset   = excluded.strings_offset,"
            "  last_ingested_at = excluded.last_ingested_at,"
            "  record_count     = excluded.record_count,"
            "  last_event_at_us = excluded.last_event_at_us,"
            "  models           = excluded.models,"
            # An alias or title arrives on one record only, so a later chunk carrying
            # none must not erase what an earlier one found.
            "  alias            = COALESCE(excluded.alias, sessions.alias),"
            "  title            = COALESCE(excluded.title, sessions.title)"
            " RETURNING id",
            (
                *key,
                chunk.messages_offset,
                chunk.strings_offset,
                time.time_ns(),
                summary.record_count,
                summary.last_event_at_us,
                json.dumps(list(summary.models)),
                summary.alias,
                summary.title,
            ),
        ).fetchone()
        return row["id"]

    @classmethod
    def _insert_blobs(cls, connection: sqlite3.Connection, chunk: IngestChunk) -> dict[bytes, int]:
        """
        Store this chunk's blobs and return a ``sha256 -> id`` map the requests can use.

        ``DO NOTHING`` because the blob store is a *set*: re-observing content is the
        normal case, not a conflict. That makes ``RETURNING`` useless for rows that
        already existed, so the ids are read back in a second pass rather than inferred
        from the insert.

        The map is keyed on every address the chunk *references*, which is deliberately
        wider than the blobs it *carries*. A session is split into chunks but its content
        is deduplicated across the whole pass, so a later chunk routinely references a
        blob an earlier chunk already committed and does not carry it again. Resolving
        only the carried blobs would raise on exactly the sessions big enough to be
        chunked.
        """
        if chunk.blobs:
            connection.executemany(
                "INSERT INTO blobs (sha256, codec, payload) VALUES (?, ?, ?)"
                " ON CONFLICT (sha256) DO NOTHING",
                [tuple(blob) for blob in chunk.blobs],
            )
        referenced = cls._referenced_addresses(chunk)
        if not referenced:
            return {}
        blob_ids: dict[bytes, int] = {}
        for batch in cls._batched(referenced):
            placeholders = ", ".join("?" * len(batch))
            rows = connection.execute(
                f"SELECT id, sha256 FROM blobs WHERE sha256 IN ({placeholders})",  # noqa: S608
                batch,
            ).fetchall()
            blob_ids.update({row["sha256"]: row["id"] for row in rows})
        return blob_ids

    @staticmethod
    def _referenced_addresses(chunk: IngestChunk) -> list[bytes]:
        """
        Every distinct blob address this chunk needs an id for.

        The union of what it carries and what its requests point at -- see
        :meth:`_insert_blobs` for why those two sets differ.
        """
        addresses = {blob.sha256 for blob in chunk.blobs}
        for record in chunk.requests:
            addresses.update(record.message_addresses)
            addresses.update(
                address
                for address in (
                    record.system_address,
                    record.tools_address,
                    record.response_address,
                )
                if address is not None
            )
        return list(addresses)

    @staticmethod
    def _batched[T: (bytes, int)](values: Sequence[T]) -> Iterator[Sequence[T]]:
        """
        Split *values* into slices that fit SQLite's bound-parameter limit.

        Addresses on the write path, blob ids on the sweep path -- both are bound into
        an ``IN`` list, and both come in counts that make an unbounded one eventually
        raise on a real host rather than in a test. A chunk of 200 records references
        several thousand distinct blobs; a sweep names every row in the store.
        """
        for start in range(0, len(values), MAX_QUERY_PARAMETERS):
            yield values[start : start + MAX_QUERY_PARAMETERS]

    @staticmethod
    def _insert_requests(
        connection: sqlite3.Connection,
        session_id: int,
        chunk: IngestChunk,
        blob_ids: dict[bytes, int],
    ) -> None:
        """
        Insert this chunk's request rows.

        Deliberately no ``ON CONFLICT``: ``UNIQUE (session_id, ordinal)`` asserts that
        ingest never misaligned against its watermark, and a conflict clause here would
        turn that bug into silent data loss.
        """

        def blob_id(address: bytes | None) -> int | None:
            return None if address is None else blob_ids[address]

        connection.executemany(
            "INSERT INTO requests ("
            "  session_id, ordinal, status, model, usage_source,"
            "  started_at_us, first_token_at_us, ended_at_us,"
            "  agent_id, finish_reason, max_tokens, error,"
            "  message_refs, system_blob, tools_blob, response_blob,"
            "  input_tokens, output_tokens, cache_write_tokens,"
            "  cache_write_5m, cache_write_1h, cache_read"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    session_id,
                    record.ordinal,
                    record.status,
                    record.model,
                    record.usage_source,
                    record.started_at_us,
                    record.first_token_at_us,
                    record.ended_at_us,
                    record.agent_id,
                    record.finish_reason,
                    record.max_tokens,
                    record.error,
                    BlobCodec.pack_ids(blob_ids[address] for address in record.message_addresses),
                    blob_id(record.system_address),
                    blob_id(record.tools_address),
                    blob_id(record.response_address),
                    record.input_tokens,
                    record.output_tokens,
                    record.cache_write_tokens,
                    record.cache_write_5m,
                    record.cache_write_1h,
                    record.cache_read,
                )
                for record in chunk.requests
            ],
        )
