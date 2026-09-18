# This file has been created with the assistance of an AI tool.
"""
The NDJSON one open session is served as, assembled from the request index.

The shape of what goes over the wire is the whole point. A conversation prefix is re-sent
every turn, so a session's messages are quadratic in its length when each record carries
its own copy: the largest session in the tree is 910 records holding 131,000 message
references behind which stand 2,442 distinct values. Sending each value once, keyed, and
the references as ``blob:<id>`` takes that response from 59 MB to 11.6 MB. Incrementally,
``from`` returns 9 to 400 KB rather than the whole session every second.

Order within the stream is a contract, not an accident -- strings before the blobs whose
text quotes them, blobs before the records that reference them -- so a consumer renders
progressively and never holds a line back waiting for content.
"""

import json
from itertools import batched
from operator import itemgetter
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.constants import HASH_POINTER_PREFIX, HASH_POINTER_RE
from agent_wrap.domain.logs.constants import (
    BLOB_LINE_TYPE,
    BLOB_REF_FORMAT,
    MICROSECONDS_PER_SECOND,
    SESSION_META_TYPE,
    SESSION_WIRE_BATCH,
)
from agent_wrap.domain.logs.normalize import enrich_with_costs, extract_record_fields

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence

    from agent_wrap.domain.logs.models import (
        CombinedSessionMeta,
        NormalizedRecord,
        NormalizedRecordBase,
    )
    from agent_wrap.domain.pricing.service import PricingService

    # TYPE_CHECKING-only, so this is not the cross-domain runtime import EA001 forbids.
    from agent_wrap.domain.providers.models import LogRecord
    from agent_wrap.infrastructure.logs.models import IndexedRequest, IndexedSession
    from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository

# One position in the order the viewer shows a session in: the sort key, the indexed
# directory the record lives in, and its ordinal within that directory. A plain tuple
# rather than a named type because it never leaves this module -- and a data class here
# would belong in models.py, which is the wrong place for a local sort key.
type _Position = tuple[int, IndexedSession, int]


class SessionStream:
    """
    Serves one session the user opened, as the lines the browser consumes.

    Holds no state between calls: every request re-reads the index, which is cheap enough
    that no session cache is needed -- the longest session in the tree is ~105 ms whole,
    and the viewer's one-second tick is single-digit milliseconds.
    """

    def __init__(
        self, request_repository: RequestRepository, pricing_service: PricingService
    ) -> None:
        self._requests = request_repository
        self._pricing = pricing_service

    def lines(
        self,
        project_hashes: Sequence[str],
        claude_session_id: str,
        meta: CombinedSessionMeta | None,
        *,
        from_index: int = 0,
        limit: int | None = None,
    ) -> Iterator[str]:
        """
        Yield the NDJSON lines for one session, without the trailing newlines.

        *meta* is passed in rather than recomputed so the header the browser draws agrees
        with the list entry the user clicked.

        *from_index* is a position in the order this yields records in. *limit* caps how
        many follow; ``session_meta`` still reports the true length, so a capped response
        is recognisable as one.
        """
        yield _line({"__type__": SESSION_META_TYPE, **(meta or {})})

        sessions = self._requests.sessions_for(project_hashes, claude_session_id)
        if not sessions:
            return
        order = self._order(sessions)
        selected = order[from_index:] if limit is None else order[from_index : from_index + limit]
        # The client holding records up to from_index also holds every blob those records
        # referenced, and the record just before the cut referenced a superset of what
        # any single earlier one did -- the prefix only grows. So excluding that one
        # record's references is enough to keep this response small, and cannot exclude
        # something the client is missing.
        sent_blobs = (
            self._references(order[from_index - 1]) if 0 < from_index <= len(order) else set()
        )
        sent_pointers: set[str] = set()
        for batch in batched(selected, SESSION_WIRE_BATCH, strict=False):
            yield from self._batch(batch, sent_blobs, sent_pointers)

    def _order(self, sessions: Sequence[IndexedSession]) -> list[_Position]:
        """
        Return every record of a merged session, in the order the viewer shows them.

        Chronological by start instant across the directories the session spans. The sort
        is stable, so records sharing an instant keep ``sessions_for``'s key order.

        A record with no start instant inherits the last one seen rather than sorting as
        zero -- otherwise a timing-less failure is hoisted to the top of the stream,
        above the conversation it preceded by milliseconds. A directory's records are
        append-ordered, so carrying the previous instant forward reproduces where the
        record actually was.
        """
        positions: list[_Position] = []
        for session in sessions:
            last = 0
            for ordinal, started_at_us in enumerate(
                self._requests.start_instants(session.session_id)
            ):
                if started_at_us is not None:
                    last = started_at_us
                positions.append((last, session, ordinal))
        positions.sort(key=itemgetter(0))
        return positions

    def _references(self, position: _Position) -> set[int]:
        """Return every blob one record points at, for the incremental set difference."""
        _key, session, ordinal = position
        row = self._requests.requests(session.session_id, [ordinal]).get(ordinal)
        return set() if row is None else _blob_ids([row])

    def _batch(
        self,
        batch: tuple[_Position, ...],
        sent_blobs: set[int],
        sent_pointers: set[str],
    ) -> Iterator[str]:
        """
        Yield one batch's content and records, content first.

        The two sets are read *and* written here, which is what makes a batch send only
        the content earlier batches did not -- without them, a batch covering record 900
        would carry the whole conversation again.
        """
        rows = self._rows(batch)
        wanted = _blob_ids(rows.values()) - sent_blobs
        responses = {row.response_blob for row in rows.values() if row.response_blob is not None}
        texts = self._requests.blob_texts(wanted | responses)

        records: list[str] = []
        for _sort_key, session, ordinal in batch:
            row = rows.get((session.session_id, ordinal))
            if row is not None:
                records.append(_line(self._record(row, session, texts)))
        blobs = [
            (BLOB_REF_FORMAT % blob_id, texts[blob_id])
            for blob_id in sorted(wanted)
            if blob_id in texts
        ]

        yield from self._pointer_lines([*(text for _ref, text in blobs), *records], sent_pointers)
        for ref, text in blobs:
            yield _blob_line(ref, text)
        sent_blobs |= wanted
        yield from records

    def _rows(self, batch: Iterable[_Position]) -> dict[tuple[int, int], IndexedRequest]:
        by_session: dict[int, list[int]] = {}
        for _key, session, ordinal in batch:
            by_session.setdefault(session.session_id, []).append(ordinal)
        return {
            (session_id, ordinal): row
            for session_id, ordinals in by_session.items()
            for ordinal, row in self._requests.requests(session_id, ordinals).items()
        }

    def _pointer_lines(self, texts: Sequence[str], sent_pointers: set[str]) -> Iterator[str]:
        """
        Yield a line for every interned string this batch's text quotes and has not sent.

        Scanned over the finished text rather than parsed values: a pointer is 69
        characters of fixed shape, and blobs are spliced onto the wire as stored JSON,
        never parsed on this path.

        Deliberately one level deep -- a pointer inside a resolved string stays literal.
        """
        pointers = {
            pointer
            for text in texts
            for pointer in HASH_POINTER_RE.findall(text)
            if pointer not in sent_pointers
        }
        if not pointers:
            return
        addresses = {bytes.fromhex(p.removeprefix(HASH_POINTER_PREFIX)): p for p in pointers}
        found = self._requests.string_texts(addresses.keys())
        # Marked as sent even when the lookup found nothing: a pointer whose original
        # never reached the index is not going to arrive on a later batch either, and
        # re-asking for it once per batch would be the only cost of pretending otherwise.
        sent_pointers |= pointers
        for address, pointer in sorted(addresses.items()):
            if address in found:
                yield _blob_line(pointer, json.dumps(found[address]))

    def _record(
        self, row: IndexedRequest, session: IndexedSession, texts: dict[int, str]
    ) -> NormalizedRecord:
        """
        Build one record in the shape the viewer consumes.

        The request side travels as references and is never parsed here. The response
        side is, through the same extraction ingest used, so the two cannot drift.
        """
        raw_response = _decode(texts.get(row.response_blob)) if row.response_blob else None
        _data, _agent_id, reply, usage, _finish = extract_record_fields(
            cast("LogRecord", {"response": raw_response})
        )
        normalized: NormalizedRecordBase = {
            "timing": {
                "start": _seconds(row.started_at_us),
                "completionStart": _seconds(row.first_token_at_us),
                "end": _seconds(row.ended_at_us),
            },
            "status": row.status,
            # Stored as "" when the record's own model was not a string at all, which is
            # the absent case the reader this replaced reported as None.
            "model": row.model or None,
            "agent_id": row.agent_id,
            "messages": [BLOB_REF_FORMAT % blob_id for blob_id in row.message_blobs],
            "system": None if row.system_blob is None else BLOB_REF_FORMAT % row.system_blob,
            "tools": [] if row.tools_blob is None else BLOB_REF_FORMAT % row.tools_blob,
            "response": reply,
            "usage": usage,
            "error": row.error,
            "finish_reason": row.finish_reason,
            "max_tokens": row.max_tokens,
        }
        # No request is passed: its cache_control markers would attribute a flat
        # cache-write total to the 1h tier, and the index does not store them. Two
        # records in the whole current tree ask for 1h, and charging them at the 5m rate
        # is what `agent stats` already does -- the two agreeing matters more than a
        # fraction of a cent, and correcting the tier is its own change.
        enriched = enrich_with_costs(normalized, raw_response, session.key.provider, self._pricing)
        return {
            **normalized,
            "context_tokens": enriched["context_tokens"],
            "output_tokens": enriched["output_tokens"],
            "cache_percent": enriched["cache_percent"],
            "cost": enriched["cost"],
        }


def _blob_line(ref: str, value_json: str) -> str:
    """
    Build a content line by splicing *value_json* in verbatim.

    A structural blob is already the canonical JSON of its value, so parsing it would
    re-serialize every byte of a multi-megabyte response for an identical result.
    ``json.dumps`` emits no bare newline, so the line cannot be broken by the splice.
    """
    return f'{{"__type__":"{BLOB_LINE_TYPE}","ref":{json.dumps(ref)},"value":{value_json}}}'


def _line(record: Mapping[str, Any]) -> str:
    return json.dumps(record, default=str)


def _blob_ids(rows: Iterable[IndexedRequest]) -> set[int]:
    found: set[int] = set()
    for row in rows:
        found.update(row.message_blobs)
        found.update(
            blob_id for blob_id in (row.system_blob, row.tools_blob) if blob_id is not None
        )
    return found


def _decode(text: str | None) -> dict[str, Any] | None:
    """Parse a stored response blob, or return None when there is nothing to parse."""
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _seconds(micros: int | None) -> float | None:
    """Convert a stored instant back to the epoch seconds the viewer's timings use."""
    return None if micros is None else micros / MICROSECONDS_PER_SECOND
