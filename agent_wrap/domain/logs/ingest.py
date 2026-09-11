# This file has been created with the assistance of an AI tool.
"""
Parsing the sidecar's JSONL log files into chunks the logs database can store.

**This module is the only reader of ``messages.jsonl`` and ``strings.jsonl``.** Every
consumer -- ``agent stats``, the usage tracker, the viewer -- reads the database
instead. That is what makes the read cost proportional to what is asked for rather
than to the whole history, and it is enforced by ``EH001`` in ``make arch-check``.

The ordering here is a correctness requirement rather than a preference. The callback
flushes a record's interned strings to ``strings.jsonl`` *before* appending the record
to ``messages.jsonl``, so reading messages up to offset *M* and only then reading
strings to EOF guarantees every pointer at or below *M* resolves. A string written
between the two reads belongs to a record above *M*, which this pass has not taken.
That invariant is what lets the schema carry no pending-pointer state at all.
"""

import hashlib
import json
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.constants import HASH_POINTER_PREFIX
from agent_wrap.domain.logs.constants import (
    MAX_CHUNK_PAYLOAD_BYTES,
    MAX_CHUNK_RECORDS,
    MESSAGES_FILENAME,
    MICROSECONDS_PER_SECOND,
    STRINGS_FILENAME,
)
from agent_wrap.domain.logs.normalize import extract_alias, extract_record_fields, extract_title
from agent_wrap.infrastructure.logs.models import (
    ContentBlob,
    IngestChunk,
    RequestRecord,
    SessionKey,
    SessionState,
    SessionSummary,
)
from agent_wrap.infrastructure.logs.repositories.ingest import BlobCodec
from agent_wrap.lib.canonical_json import canonical_bytes
from agent_wrap.lib.log_records import usage_source

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    # TYPE_CHECKING-only, so this is not the cross-domain runtime import EA001 forbids.
    from agent_wrap.domain.providers.models import LogRecord


def discover_sessions(logs_root: Path) -> list[tuple[SessionKey, Path]]:
    """
    Return every session directory under *logs_root*, keyed and sorted.

    The tree is ``<logs_root>/<project_hash>/<provider>/<claude_session_id>/`` — the
    layout :class:`SessionKey` mirrors — so the walk is three fixed levels deep rather
    than an ``os.walk``. Anything that is not a directory at the depth it should be, or
    that holds no ``messages.jsonl``, is skipped rather than guessed at.

    Walking the *central* tree rather than each project's ``.claude/litellm-logs``
    symlink is deliberate. The central tree is the one the sidecars actually write, it
    names each project's hash directly, and it includes the dirs of deleted projects —
    which is what lets ``agent cleanup`` and the stats orphan bucket agree with the
    index instead of diverging from it.

    Sorted so a backfill's progress is reproducible and two runs report the same order.
    An enumeration is not a read: this opens no log file, so the constraint that only
    this module does is untouched.
    """
    if not logs_root.is_dir():
        return []
    found: list[tuple[SessionKey, Path]] = []
    for project_dir in _subdirectories(logs_root):
        for provider_dir in _subdirectories(project_dir):
            for session_dir in _subdirectories(provider_dir):
                if not LogFiles.messages(session_dir).is_file():
                    continue
                key = SessionKey(
                    project_hash=project_dir.name,
                    provider=provider_dir.name,
                    claude_session_id=session_dir.name,
                )
                found.append((key, session_dir))
    # Sorting the pairs sorts by SessionKey: the key is unique per directory, so the
    # path never has to break a tie and the order is the tree's own.
    return sorted(found)


def _subdirectories(parent: Path) -> list[Path]:
    """Return *parent*'s immediate subdirectories, sorted, or ``[]`` if it is unreadable."""
    try:
        entries = sorted(parent.iterdir())
    except OSError:
        # A directory removed or made unreadable mid-walk is not an ingest failure --
        # it is a session that no longer exists, which the next pass simply will not see.
        return []
    return [entry for entry in entries if entry.is_dir()]


class LogFiles:
    """Locating and sanity-checking the two files that make up a session on disk."""

    @staticmethod
    def messages(session_dir: Path) -> Path:
        return session_dir / MESSAGES_FILENAME

    @staticmethod
    def strings(session_dir: Path) -> Path:
        return session_dir / STRINGS_FILENAME

    @staticmethod
    def is_messages(path: Path) -> bool:
        """
        Report whether *path* names a session's record file.

        Asked by the two callers that route filesystem events -- the watcher, deciding
        what to forward, and the cache, mapping a forwarded path to a group. Neither
        opens the file, and both used to compare against the filename constant
        themselves. The predicate lives here so that nothing outside this module names a
        log file at all, which is exactly what ``EH001`` in ``make arch-check`` asserts:
        a rule about a *name* is checkable, where "does this read the file" is not.
        """
        return path.name == MESSAGES_FILENAME

    @staticmethod
    def is_truncated(session_dir: Path, state: SessionState) -> bool:
        """
        Report whether either file is now smaller than its own watermark.

        Both files are strictly append-only, so this can only mean the file was
        replaced rather than appended to -- at which point every stored offset is
        meaningless and the session has to be re-ingested from zero. A missing file is
        not truncation: it is a session that has been deleted out of band, which the
        directory walk handles.
        """
        for path, offset in (
            (LogFiles.messages(session_dir), state.messages_offset),
            (LogFiles.strings(session_dir), state.strings_offset),
        ):
            if offset and path.is_file() and path.stat().st_size < offset:
                return True
        return False


class RecordParser:
    """Turning one raw log record into the row and blobs the database stores."""

    @staticmethod
    def to_micros(value: object) -> int | None:
        """
        Convert an epoch-seconds timing to integer microseconds.

        Integers rather than floats so ordering and windowing are exact. ``None`` is
        preserved rather than defaulted, because a record with no timing feeds the
        stats layer's "?" day key and must not be given a fabricated instant.
        """
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return None
        return round(value * MICROSECONDS_PER_SECOND)

    @staticmethod
    def cache_split(usage: dict[str, Any]) -> tuple[int, int]:
        """
        Return the ``(5m, 1h)`` ephemeral cache-write split, or ``(0, 0)``.

        Read from ``prompt_tokens_details.cache_creation_token_details`` and nowhere
        else: across the whole current log tree, every one of the 19,296 records that
        carries a split reports it only there. Storing it does not change today's
        arithmetic -- no consumer reads it yet -- but it is what makes the separate
        cache-tier fix possible without a re-ingest.
        """
        details = usage.get("prompt_tokens_details")
        if not isinstance(details, dict):
            return 0, 0
        split = details.get("cache_creation_token_details")
        if not isinstance(split, dict):
            return 0, 0
        return (
            _non_negative_int(split.get("ephemeral_5m_input_tokens")),
            _non_negative_int(split.get("ephemeral_1h_input_tokens")),
        )

    @staticmethod
    def parse(rec: LogRecord, ordinal: int, blobs: _BlobAccumulator) -> RequestRecord:
        """Reduce one record to a :class:`RequestRecord`, interning its content in *blobs*."""
        data, agent_id, _reply, usage, finish_reason = extract_record_fields(rec)
        timing = rec.get("timing") or {}

        raw_max_tokens = data.get("max_tokens")
        # bool is an int subclass, and a stray True would render as a cap of 1.
        max_tokens = (
            raw_max_tokens
            if isinstance(raw_max_tokens, int) and not isinstance(raw_max_tokens, bool)
            else None
        )

        cache_write = _non_negative_int(usage.get("cache_creation_input_tokens"))
        cache_5m, cache_1h = RecordParser.cache_split(usage)
        # The CHECK constraint refuses a split wider than the total it splits. A record
        # reporting one is self-inconsistent, and the flat total is the figure every
        # consumer actually charges, so the split is dropped rather than the record.
        if cache_5m + cache_1h > cache_write:
            cache_5m, cache_1h = 0, 0

        model = rec.get("model")
        return RequestRecord(
            ordinal=ordinal,
            status="success" if rec.get("status") == "success" else "failure",
            # Stored verbatim, and "" when absent rather than a placeholder that could
            # collide with a real model name. The stats fold skips those, exactly as
            # accumulate_record's `if not model` does today.
            model=model if isinstance(model, str) else "",
            usage_source=usage_source(rec),
            started_at_us=RecordParser.to_micros(timing.get("start")),
            first_token_at_us=RecordParser.to_micros(timing.get("completionStart")),
            ended_at_us=RecordParser.to_micros(timing.get("end")),
            agent_id=agent_id,
            finish_reason=finish_reason,
            max_tokens=max_tokens,
            error=rec.get("error") if isinstance(rec.get("error"), str) else None,
            message_addresses=tuple(blobs.add_value(m) for m in (data.get("messages") or [])),
            system_address=blobs.add_optional(data.get("system")),
            tools_address=blobs.add_optional(data.get("tools") or None),
            response_address=blobs.add_optional(rec.get("response")),
            input_tokens=_non_negative_int(usage.get("input_tokens") or usage.get("prompt_tokens")),
            output_tokens=_non_negative_int(
                usage.get("output_tokens") or usage.get("completion_tokens")
            ),
            cache_write_tokens=cache_write,
            cache_write_5m=cache_5m,
            cache_write_1h=cache_1h,
            cache_read=_non_negative_int(usage.get("cache_read_input_tokens")),
        )


class _BlobAccumulator:
    """
    Collects the distinct content a pass has seen, addressed and already encoded.

    Deduplication happens here rather than in SQL because it is what keeps the work
    proportional: a conversation prefix is re-sent on every turn, so one session's
    3,000 message references resolve to a few hundred distinct values, and only those
    are ever compressed or handed to the database.

    The ``seen`` set spans the whole session pass, not one chunk, so the prefix is
    encoded once no matter how many chunks the session is split across.
    """

    def __init__(self) -> None:
        self._seen: set[bytes] = set()
        self._pending: list[ContentBlob] = []
        self._pending_bytes = 0

    def add_value(self, value: object) -> bytes:
        """Intern a structural value and return its content address."""
        raw = canonical_bytes(value)
        return self._intern(hashlib.sha256(raw).digest(), raw)

    def add_optional(self, value: object) -> bytes | None:
        """Intern *value* unless it is absent, in which case the column stays NULL."""
        return None if value is None else self.add_value(value)

    def add_interned_string(self, pointer: str, original: str) -> None:
        """
        Intern a string the sidecar already hashed, addressed by its own pointer.

        The pointer *is* the content address -- ``StringHasher`` computes an unsalted
        global SHA-256 -- so the same string interned in two different sessions
        collapses to one row. The digest is recomputed rather than trusted: a corrupt
        line would otherwise store content under an address nothing resolves to.
        """
        raw = original.encode("utf-8", errors="surrogatepass")
        address = hashlib.sha256(raw).digest()
        if address.hex() != pointer.removeprefix(HASH_POINTER_PREFIX):
            return
        self._intern(address, raw)

    def _intern(self, address: bytes, raw: bytes) -> bytes:
        if address not in self._seen:
            self._seen.add(address)
            codec, payload = BlobCodec.encode(raw)
            self._pending.append(ContentBlob(sha256=address, codec=codec, payload=payload))
            self._pending_bytes += len(payload)
        return address

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    def take(self) -> tuple[ContentBlob, ...]:
        """Hand over everything accumulated since the last call, and reset."""
        taken = tuple(self._pending)
        self._pending = []
        self._pending_bytes = 0
        return taken


def _non_negative_int(value: object) -> int:
    """Coerce a reported token count to a non-negative int, defaulting to zero."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return 0
    return value


def read_ingest_chunks(session_dir: Path, state: SessionState) -> Iterator[IngestChunk]:
    """
    Yield the chunks needed to bring *session_dir* up to date from *state*.

    Messages first, then strings -- see the module docstring; the order is what makes
    unresolved pointers impossible. A trailing partial line is never consumed, so an
    append torn by a crash is simply picked up on the next pass.

    Yields nothing when there is no new content, which is the common case on a
    heartbeat: the watermarks already sit at both files' ends.
    """
    messages_path = LogFiles.messages(session_dir)
    if not messages_path.is_file():
        return

    blobs = _BlobAccumulator()
    summary = _SummaryAccumulator(state.summary)
    records: list[RequestRecord] = []
    ordinal = state.summary.record_count
    offset = state.messages_offset

    with messages_path.open("rb") as handle:
        handle.seek(offset)
        for raw_line in handle:
            if not raw_line.endswith(b"\n"):
                # A torn final append. Leave it for next time rather than parsing a
                # fragment; the watermark stops short of it, so nothing is lost.
                break
            offset += len(raw_line)
            record = _parse_line(raw_line, ordinal, blobs, summary)
            if record is None:
                continue
            records.append(record)
            ordinal += 1
            if len(records) >= MAX_CHUNK_RECORDS or blobs.pending_bytes >= MAX_CHUNK_PAYLOAD_BYTES:
                # Strings are read only for the final chunk, so intermediate chunks
                # carry the strings watermark unchanged and the invariant still holds:
                # every pointer they reference is resolved by the time the pass ends.
                yield IngestChunk(
                    blobs=blobs.take(),
                    requests=tuple(records),
                    summary=summary.snapshot(),
                    messages_offset=offset,
                    strings_offset=state.strings_offset,
                )
                records = []

    strings_offset = _read_strings(session_dir, state.strings_offset, blobs)
    if not records and not blobs.pending_bytes and strings_offset == state.strings_offset:
        return

    yield IngestChunk(
        blobs=blobs.take(),
        requests=tuple(records),
        summary=summary.snapshot(),
        messages_offset=offset,
        strings_offset=strings_offset,
    )


def _parse_line(
    raw_line: bytes, ordinal: int, blobs: _BlobAccumulator, summary: _SummaryAccumulator
) -> RequestRecord | None:
    """Parse one complete line, or return ``None`` if it is blank or malformed."""
    stripped = raw_line.strip()
    if not stripped:
        return None
    try:
        parsed = json.loads(stripped)
    except ValueError:
        # Matches the reader this replaces: a corrupt line is skipped, not fatal.
        return None
    if not isinstance(parsed, dict):
        return None
    rec = cast("LogRecord", parsed)
    summary.observe(rec)
    return RecordParser.parse(rec, ordinal, blobs)


def _read_strings(session_dir: Path, offset: int, blobs: _BlobAccumulator) -> int:
    """
    Intern every string appended to ``strings.jsonl`` past *offset*, and return the new one.

    Read to EOF deliberately. Reading further than the messages watermark is safe and
    is the point: strings are always flushed ahead of the record that references them,
    so anything extra here belongs to a record a later pass will take.
    """
    strings_path = LogFiles.strings(session_dir)
    if not strings_path.is_file():
        return offset
    with strings_path.open("rb") as handle:
        handle.seek(offset)
        for raw_line in handle:
            if not raw_line.endswith(b"\n"):
                break
            offset += len(raw_line)
            stripped = raw_line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            pointer = entry.get("hash")
            original = entry.get("original")
            if isinstance(pointer, str) and isinstance(original, str):
                blobs.add_interned_string(pointer, original)
    return offset


class _SummaryAccumulator:
    """
    Maintains the five columns ``meta.json`` used to cache, carried forward per pass.

    Mirrors ``io._accumulate_session_meta`` exactly, including that ``last_event_at``
    is the *last* record's end rather than the maximum -- the file is append-ordered,
    so the two agree, but reproducing the rule keeps the cutover bit-identical.
    """

    def __init__(self, prior: SessionSummary) -> None:
        self._count = prior.record_count
        self._last_event_at_us = prior.last_event_at_us
        self._models = set(prior.models)
        self._alias = prior.alias
        self._title = prior.title

    def observe(self, rec: LogRecord) -> None:
        self._count += 1
        end = RecordParser.to_micros((rec.get("timing") or {}).get("end"))
        if end is not None:
            self._last_event_at_us = end
        model = rec.get("model")
        if isinstance(model, str):
            self._models.add(model.rsplit("/", 1)[-1])
        alias = extract_alias(rec)
        if alias:
            self._alias = alias
        title = extract_title(rec)
        if title:
            self._title = title

    def snapshot(self) -> SessionSummary:
        return SessionSummary(
            record_count=self._count,
            last_event_at_us=self._last_event_at_us,
            models=tuple(sorted(self._models)),
            alias=self._alias,
            title=self._title,
        )
