# This file has been created with the assistance of an AI tool.
"""The logs database's content read side: what the viewer's session view asks for."""

from itertools import batched
from typing import TYPE_CHECKING

from agent_wrap.infrastructure.logs.constants import MAX_QUERY_PARAMETERS
from agent_wrap.infrastructure.logs.models import IndexedRequest, IndexedSession, SessionKey
from agent_wrap.infrastructure.logs.repositories.ingest import BlobCodec

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Sequence

    from agent_wrap.infrastructure.connection import ConnectionFactory


class RequestRepository:
    """
    Reads one session's requests and the content they point at.

    The counterpart to :class:`~agent_wrap.infrastructure.logs.repositories.usage.UsageRepository`:
    that one may only aggregate, this one is the single place allowed to return
    ``requests`` a row at a time -- and only for one session the user has opened, which
    is a few hundred rows rather than the whole history.

    Every method takes the identifiers it needs and opens its own connection. That is
    not an oversight: a ``ro()`` open costs about a sixth of a millisecond, while the
    thing the batching *does* buy -- a blob fetch scoped to the content a batch has not
    already sent -- is what keeps the viewer's one-second tick a few kilobytes.

    Content comes back as decoded text, never as a payload plus a codec. What that text
    means follows from how the caller reached it, exactly as the schema's missing
    ``kind`` column intends: canonical JSON for a structural value, the string itself
    for an interned one.
    """

    def __init__(self, connection_factory: ConnectionFactory) -> None:
        self._connections = connection_factory

    def sessions_for(
        self, project_hashes: Sequence[str], claude_session_id: str
    ) -> list[IndexedSession]:
        """
        Return every indexed directory that holds part of one session the user sees.

        More than one when the session switched provider mid-flight, or when two member
        projects of a grouped transient project share it -- which is why *project_hashes*
        is a list. Sorted by key so the merge that follows is reproducible; the tree's
        own directory order is ``iterdir()``'s, which is not.

        Batched over the hashes because a grouped transient project can have thousands
        of members, and an unbounded ``IN`` list would eventually raise on a real host
        rather than in a test.
        """
        if not project_hashes:
            return []
        found: list[IndexedSession] = []
        with self._connections.ro() as connection:
            for hashes in batched(project_hashes, MAX_QUERY_PARAMETERS - 1, strict=False):
                placeholders = ", ".join("?" * len(hashes))
                rows = connection.execute(
                    "SELECT id, project_hash, provider, record_count"  # noqa: S608
                    "  FROM sessions"
                    f" WHERE claude_session_id = ? AND project_hash IN ({placeholders})",
                    (claude_session_id, *hashes),
                ).fetchall()
                found.extend(
                    IndexedSession(
                        key=SessionKey(
                            project_hash=row["project_hash"],
                            provider=row["provider"],
                            claude_session_id=claude_session_id,
                        ),
                        session_id=row["id"],
                        record_count=row["record_count"],
                    )
                    for row in rows
                )
        return sorted(found)

    def start_instants(self, session_id: int) -> list[int | None]:
        """
        Return every request's start instant, in ``ordinal`` order.

        The narrow pass that decides what order the viewer shows a session in, ahead of
        reading any content. A request that carried no timing at all is ``None`` here
        rather than zero: what to do with one is the reader's rule, and it is not "sort
        it to the top" -- see the domain's order-key pass.

        Ordinals are contiguous from zero, so the position in this list *is* the
        ordinal and nothing has to be returned alongside it.
        """
        with self._connections.ro() as connection:
            rows = connection.execute(
                "SELECT started_at_us FROM requests WHERE session_id = ? ORDER BY ordinal",
                (session_id,),
            ).fetchall()
        return [row["started_at_us"] for row in rows]

    def requests(self, session_id: int, ordinals: Sequence[int]) -> dict[int, IndexedRequest]:
        """
        Return the named requests of one session, keyed by ordinal.

        Keyed rather than ordered because the caller asked for a set of ordinals and
        already knows what order it wants them in -- which is not ``ordinal`` order once
        the start instants have been sorted.
        """
        if not ordinals:
            return {}
        found: dict[int, IndexedRequest] = {}
        with self._connections.ro() as connection:
            for wanted in batched(ordinals, MAX_QUERY_PARAMETERS - 1, strict=False):
                placeholders = ", ".join("?" * len(wanted))
                rows = connection.execute(
                    "SELECT ordinal, status, model,"  # noqa: S608
                    "       started_at_us, first_token_at_us, ended_at_us,"
                    "       agent_id, finish_reason, max_tokens, error,"
                    "       message_refs, system_blob, tools_blob, response_blob"
                    "  FROM requests"
                    f" WHERE session_id = ? AND ordinal IN ({placeholders})",
                    (session_id, *wanted),
                ).fetchall()
                found.update(
                    {
                        row["ordinal"]: IndexedRequest(
                            ordinal=row["ordinal"],
                            status=row["status"],
                            model=row["model"],
                            started_at_us=row["started_at_us"],
                            first_token_at_us=row["first_token_at_us"],
                            ended_at_us=row["ended_at_us"],
                            agent_id=row["agent_id"],
                            finish_reason=row["finish_reason"],
                            max_tokens=row["max_tokens"],
                            error=row["error"],
                            message_blobs=BlobCodec.unpack_ids(row["message_refs"]),
                            system_blob=row["system_blob"],
                            tools_blob=row["tools_blob"],
                            response_blob=row["response_blob"],
                        )
                        for row in rows
                    }
                )
        return found

    def blob_texts(self, blob_ids: Collection[int]) -> dict[int, str]:
        return dict(self._texts("id", blob_ids))

    def string_texts(self, addresses: Collection[bytes]) -> dict[bytes, str]:
        """
        Return the decoded text of each blob named by its content address.

        The lookup an interned ``hash:`` pointer needs: the pointer's 64 hex characters
        *are* the address, because the sidecar's hasher computes an unsalted global
        SHA-256 and ingest stores the string under exactly that digest. An address with
        no row is simply absent from the result -- a pointer whose original never
        reached ``strings.jsonl`` intact has no content to serve, and the reader shows
        the pointer rather than inventing one.
        """
        return dict(self._texts("sha256", addresses))

    def _texts[K: (int, bytes)](self, column: str, keys: Collection[K]) -> Iterator[tuple[K, str]]:
        """
        Yield ``(key, decoded text)`` for every blob matched on *column*.

        Decoded with ``errors="replace"``. Ingest encodes with ``surrogatepass`` so that
        a lone surrogate in a log record cannot fail the whole pass, which means a blob's
        bytes are not always valid UTF-8 -- and every consumer of this text puts it on a
        UTF-8 wire, where a surviving surrogate would fail the response instead.
        """
        if not keys:
            return
        with self._connections.ro() as connection:
            for wanted in batched(keys, MAX_QUERY_PARAMETERS, strict=False):
                placeholders = ", ".join("?" * len(wanted))
                rows = connection.execute(
                    f"SELECT {column} AS key, codec, payload FROM blobs"  # noqa: S608
                    f" WHERE {column} IN ({placeholders})",
                    tuple(wanted),
                ).fetchall()
                for row in rows:
                    raw = BlobCodec.decode(row["codec"], row["payload"])
                    yield row["key"], raw.decode("utf-8", errors="replace")
