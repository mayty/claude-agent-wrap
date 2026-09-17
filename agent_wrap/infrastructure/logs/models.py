# This file has been created with the assistance of an AI tool.
"""
Data models for the logs database.

Value types, not repositories: the domain's ingest parser imports them at runtime, which
rule 2 in ``docs/infrastructure.md`` permits precisely because they carry no behaviour and
no connection.
"""

from typing import NamedTuple


class SessionKey(NamedTuple):
    """
    The triple, not the session id alone: a session that switched provider mid-flight has
    one directory per provider. Merging them back is read-time policy.
    """

    project_hash: str
    provider: str
    claude_session_id: str


class Revision(NamedTuple):
    """
    ``last_ingested_ns`` moves on every ingest pass *and* on a metadata-only update, while
    ``count`` catches a session appearing or being deleted -- so the pair detects every
    change the viewer can show, in one B-tree probe.
    """

    last_ingested_ns: int | None
    count: int


class IndexFootprint(NamedTuple):
    """
    ``database_bytes`` is page count times page size, asked of SQLite rather than the
    filesystem, so the figure holds wherever the WAL has got to. ``last_ingested_ns`` is
    ``None`` where nothing has been ingested -- not the same as an up-to-date index.
    """

    database_bytes: int
    sessions: int
    requests: int
    last_ingested_ns: int | None


class SessionRow(NamedTuple):
    """
    Per *directory*, not per session: a provider switch mid-flight yields one row per
    provider, and merging them is read-time policy in the domain. ``last_event_at_us`` is
    the last record's ``timing.end``, or ``None`` where every record lacked one.
    """

    key: SessionKey
    last_ingested_ns: int
    record_count: int
    last_event_at_us: int | None
    models: tuple[str, ...]
    alias: str | None
    title: str | None


class IndexedSession(NamedTuple):
    """
    ``session_id`` is what ``requests`` is keyed by, so it is the only handle a content read
    can use; ``key`` carries the triple the tree spells it with, whose ``provider`` the
    caller needs to price the requests.
    """

    key: SessionKey
    session_id: int
    record_count: int


class IndexedRequest(NamedTuple):
    """
    Content left as blob ids, which keeps the viewer's session read proportional to the
    session rather than its square: a conversation prefix is re-sent every turn, so the ids
    repeat while the content behind them is fetched once. ``message_blobs`` is ordered and
    may repeat; a ``None`` optional column differs from an empty one.
    """

    ordinal: int
    status: str
    model: str
    started_at_us: int | None
    first_token_at_us: int | None
    ended_at_us: int | None
    agent_id: str | None
    finish_reason: str | None
    max_tokens: int | None
    error: str | None
    message_blobs: tuple[int, ...]
    system_blob: int | None
    tools_blob: int | None
    response_blob: int | None


class SessionWatermark(NamedTuple):
    """
    Returned rather than acted on: the comparison that matters is against the file's live
    ``st_size``, and the storage layer has no business in the log tree.
    """

    key: SessionKey
    messages_offset: int


class UsageCell(NamedTuple):
    """
    One pre-summed ``(utc hour, session, model, usage source)`` group of requests.

    Fine enough that pricing can charge each hour at its own rate, coarse enough that the
    whole history collapses from ~44k rows to ~1.8k cells. Because it is pre-summed
    (``requests`` may be > 1) a consumer must *merge* cells into an accumulator, not add
    them one at a time.

    ``provider`` is the *sidecar's* name (``litellm-bedrock``), not the vendor prefix inside
    ``model``. ``hour_bucket`` counts hours since the epoch, ``None`` for an untimed request;
    no calendar field is stored, so the date, weekday and ``DAY_START_HOURS``-shifted stats
    day are all derived at read time.
    """

    hour_bucket: int | None
    project_hash: str
    provider: str
    session_id: int
    model: str
    usage_source: str
    requests: int
    last_started_at_us: int | None
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int


class ContentBlob(NamedTuple):
    """
    Addressed by the SHA-256 of its canonical bytes. ``payload`` is what the ``blobs`` row
    stores, still encoded per ``codec``: decoding is the repository's job on the way out,
    while on the way in the parser has already chosen the codec.
    """

    sha256: bytes
    codec: str
    payload: bytes


class BlobSweep(NamedTuple):
    """
    ``freed_bytes`` is the stored size of the deleted payloads, not the shrinkage of the
    database file -- a freed page returns to the filesystem only as far as the incremental
    vacuum reaches.
    """

    removed: int
    freed_bytes: int


class RequestRecord(NamedTuple):
    """
    One parsed line of ``messages.jsonl``. Blob-valued fields are carried as *addresses*,
    not ids: the parser runs in a worker process with no database handle, so it cannot know
    what id a blob will get.
    """

    ordinal: int
    status: str
    model: str
    usage_source: str
    started_at_us: int | None
    first_token_at_us: int | None
    ended_at_us: int | None
    agent_id: str | None
    finish_reason: str | None
    max_tokens: int | None
    error: str | None
    message_addresses: tuple[bytes, ...]
    system_address: bytes | None
    tools_address: bytes | None
    response_address: bytes | None
    input_tokens: int
    output_tokens: int
    cache_write_tokens: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int


class SessionSummary(NamedTuple):
    """
    Cumulative rather than per-chunk, so the upsert can assign rather than accumulate and
    re-ingesting a chunk cannot double-count.
    """

    record_count: int
    last_event_at_us: int | None
    models: tuple[str, ...]
    alias: str | None
    title: str | None


class SessionState(NamedTuple):
    """
    The watermarks say where to resume. The summary rides along because its columns are
    cumulative over the session while a pass sees only records after the watermark --
    handing the prior totals to the parser keeps the SQL free of accumulation.
    """

    messages_offset: int
    strings_offset: int
    summary: SessionSummary

    @classmethod
    def unseen(cls) -> SessionState:
        return cls(
            messages_offset=0,
            strings_offset=0,
            summary=SessionSummary(
                record_count=0, last_event_at_us=None, models=(), alias=None, title=None
            ),
        )


class IngestChunk(NamedTuple):
    """
    One transactional unit of ingest. Bounded by the parser because the repository writes
    the whole chunk in one transaction -- a 68 MB session in one go would build a WAL larger
    than the session and lose all of it on interruption. The offsets are absolute positions
    *after* this chunk's lines, so applying it and advancing the watermarks is one step.
    """

    blobs: tuple[ContentBlob, ...]
    requests: tuple[RequestRecord, ...]
    summary: SessionSummary
    messages_offset: int
    strings_offset: int
