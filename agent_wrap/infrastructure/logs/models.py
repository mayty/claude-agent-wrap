# This file has been created with the assistance of an AI tool.
"""
Data models for the logs database.

These are value types, not repositories. The domain's ingest parser imports them
directly and at runtime, which is permitted precisely because they carry no behaviour
and no connection -- the dependency still points downward, and the repository itself
still arrives by constructor injection. See rule 2 in ``docs/infrastructure.md``.
"""

from typing import NamedTuple


class SessionKey(NamedTuple):
    """
    The identity of one on-disk session directory.

    The triple, not the session id alone: a session that switched provider mid-flight
    has one directory per provider, and each is ingested separately. Merging them back
    into the single session a user sees is read-time policy, so nothing here does it.

    Mirrors ``TOOL_DIR/litellm-logs/<project_hash>/<provider>/<claude_session_id>/`` and
    the ``sessions`` table's UNIQUE constraint, in that order.
    """

    project_hash: str
    provider: str
    claude_session_id: str


class Revision(NamedTuple):
    """
    A cheap summary of the sessions table, for change detection and freshness.

    The same trick ``projects.Revision`` plays on the registry: ``last_ingested_ns``
    moves on every ingest pass *and* on a metadata-only update (an alias arriving on
    record 1 rewrites the row), while ``count`` catches a session appearing or being
    deleted. Together they replace the viewer's per-file ``stat()`` sweep with one
    B-tree probe. ``None`` for ``last_ingested_ns`` means nothing has been ingested yet.
    """

    last_ingested_ns: int | None
    count: int


class IndexFootprint(NamedTuple):
    """
    How big the index is and how fresh it is, for a report rather than a decision.

    ``database_bytes`` is the database's own logical size -- page count times page
    size, asked of SQLite rather than of the filesystem, so the figure holds whatever
    the file is called and wherever the WAL has got to.

    ``requests`` is summed from the sessions table's own counter rather than counted
    over ``requests``, which is both cheaper and the number the sessions list already
    agrees with. ``last_ingested_ns`` is ``None`` on a host where nothing has been
    ingested yet -- which is not the same as an index that is up to date.
    """

    database_bytes: int
    sessions: int
    requests: int
    last_ingested_ns: int | None


class SessionRow(NamedTuple):
    """
    One indexed session directory, as the viewer's session list needs it.

    The five summary fields are exactly what ``meta.json`` used to cache, plus the
    row's own ``last_ingested_ns`` -- which is what makes a fingerprint out of a list
    of these without a second query.

    Per *directory*, not per session: the triple in ``key`` is the identity, and a
    session that switched provider mid-flight has one row per provider. Merging them
    into the single entry a user sees is read-time policy and happens in the domain.

    ``last_event_at_us`` is the last record's ``timing.end`` in epoch microseconds, or
    ``None`` for a session whose records all lacked one.
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
    One indexed session directory, identified by both of its identities.

    ``session_id`` is the ``sessions`` row id, which is what ``requests`` is keyed by
    and therefore the only handle a content read can use. ``key`` carries the triple
    the tree spells it with -- the caller needs ``provider`` to price the requests, and
    it is the merge order across a session that switched provider mid-flight.
    """

    key: SessionKey
    session_id: int
    record_count: int


class IndexedRequest(NamedTuple):
    """
    One request row, with its content left as blob ids rather than content.

    The split is what makes the viewer's session read proportional to the session
    rather than to its square: a conversation prefix is re-sent on every turn, so the
    ids repeat while the content behind them is fetched and sent exactly once.

    ``message_blobs`` is ordered and may repeat -- it is the message order the viewer
    renders. The three optional columns are ``None`` where the request carried no such
    value at all, which is different from an empty one.

    ``usage`` is deliberately absent: the numbers a viewer shows come out of the
    response blob verbatim, and the summed columns exist for the stats aggregate, which
    reads them through :class:`UsageCell` and never a row at a time.
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
    How far ingest has read into one session's messages file.

    Returned rather than acted on because the comparison that matters -- watermark
    against the file's live ``st_size`` -- is a filesystem question, and the storage
    layer has no business in the log tree. The domain stats the file and decides.
    """

    key: SessionKey
    messages_offset: int


class UsageCell(NamedTuple):
    """
    One pre-summed ``(utc hour, session, model, usage source)`` group of requests.

    The grain the stats aggregate returns: fine enough that pricing can charge each
    hour at its own rate and that ``usage_source`` stays a group rather than a
    per-record flag, coarse enough that the whole history collapses from ~44k rows to
    ~1.8k cells.

    ``project_hash`` and ``provider`` come from the session the requests belong to, not
    from the requests themselves. ``provider`` is the *sidecar's* name
    (``litellm-bedrock``), which is what the model is displayed under -- the upstream
    vendor prefix inside ``model`` is a different thing and is not it.

    ``hour_bucket`` is ``started_at_us / 3600000000`` -- an integer count of hours since
    the epoch, and ``None`` for a request that carried no timestamp. No calendar field
    is stored or returned: the UTC date, the weekday and the ``DAY_START_HOURS``-shifted
    stats day are all derived by the caller, from this bucket, with whatever the
    environment says at read time. ``last_started_at_us`` is the exact newest instant in
    the cell, which the bucket is deliberately too coarse to give.

    Pre-summed is the distinction from a record: a consumer must merge these into an
    accumulator rather than add them one at a time, since ``requests`` may be > 1.
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
    One distinct piece of content, addressed by the SHA-256 of its canonical bytes.

    ``payload`` is what the ``blobs`` row stores, still encoded per ``codec``. Decoding
    is the repository's job on the way out; on the way in the parser has already chosen
    the codec, so the same shape serves both directions.
    """

    sha256: bytes
    codec: str
    payload: bytes


class BlobSweep(NamedTuple):
    """
    What a blob sweep reclaimed: how many rows went, and the bytes they stored.

    ``freed_bytes`` is the stored size of the deleted payloads, not the shrinkage of
    the database file. The two differ -- a freed page is only returned to the
    filesystem as far as the incremental vacuum reaches, and an index entry goes with
    each row on top of its payload -- and the payload total is the figure that means
    something to a reader: it is the content that is gone.
    """

    removed: int
    freed_bytes: int


class RequestRecord(NamedTuple):
    """
    One parsed line of ``messages.jsonl`` -- one upstream LLM call, success or failure.

    Blob-valued fields are carried as *addresses*, not ids: the parser runs in a worker
    process that has no database handle, so it cannot know what id a blob will get. The
    repository resolves each address to an id as it writes, which is also what lets it
    keep an in-process LRU in front of the lookup.

    ``message_addresses`` is ordered and may repeat -- a conversation prefix is re-sent
    on every turn, and the order is the message order the viewer renders.
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
    The five fields ``meta.json`` used to cache, recomputed as a session is ingested.

    Cumulative rather than per-chunk: ``record_count`` counts the whole session and
    ``models`` lists everything seen in it, so the upsert can assign rather than
    accumulate and re-ingesting a chunk cannot double-count.
    """

    record_count: int
    last_event_at_us: int | None
    models: tuple[str, ...]
    alias: str | None
    title: str | None


class SessionState(NamedTuple):
    """
    Everything ingest needs to know about a session before reading another byte.

    The two watermarks say where to resume; the summary is carried because the summary
    columns are *cumulative over the session* while a pass only ever sees the records
    after the watermark. Handing the prior totals to the parser lets it emit cumulative
    figures the upsert can assign, which keeps the SQL free of accumulation and makes a
    replayed chunk arithmetically harmless.
    """

    messages_offset: int
    strings_offset: int
    summary: SessionSummary

    @classmethod
    def unseen(cls) -> SessionState:
        """Return the state of a session that has never been ingested."""
        return cls(
            messages_offset=0,
            strings_offset=0,
            summary=SessionSummary(
                record_count=0, last_event_at_us=None, models=(), alias=None, title=None
            ),
        )


class IngestChunk(NamedTuple):
    """
    One transactional unit of ingest: blobs, requests, the summary, and both watermarks.

    Bounded by the parser (a record count and a payload budget) because the repository
    writes the whole chunk in a single transaction -- ingesting a 68 MB session in one
    go would build a WAL larger than the session and lose all of it on interruption.

    The offsets are absolute positions in the two source files *after* this chunk's
    lines, so applying the chunk and advancing the watermarks is one atomic step.
    """

    blobs: tuple[ContentBlob, ...]
    requests: tuple[RequestRecord, ...]
    summary: SessionSummary
    messages_offset: int
    strings_offset: int
