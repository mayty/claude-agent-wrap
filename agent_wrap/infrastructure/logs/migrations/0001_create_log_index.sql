-- One row per on-disk session directory:
-- TOOL_DIR/litellm-logs/<project_hash>/<provider>/<session_id>/
--
-- The natural key is that triple, not the session id alone: a session that switched
-- provider mid-flight has one directory per provider, and the viewer *merges* them at
-- read time -- a merge that also spans the member projects of a grouped transient
-- project. Merging is read-time policy, so this table stores the directory, not the
-- merged view.
--
-- `project_hash` is stored; the project *path* is not. The hash is a one-way function of
-- the resolved path, the registry lives in another database, and "orphaned" is a
-- question about the registry *now* -- a project can be re-registered. Resolving
-- hash -> path is a read-time dict comprehension over ~29 hashes in the domain, not a
-- stale copy here.
--
-- THE WATERMARKS. Two byte offsets, inline, because there are exactly two ingested files
-- per session and there always will be. The callback writes a third, meta.json, which
-- this table replaces outright (see the summary columns below) and which ingest
-- therefore never reads.
--
-- A byte offset is a valid watermark only because both files are strictly append-only --
-- the callback opens them "a" and writes one line per call. Two consequences:
--   * an offset always points just past a newline. A large write can be torn by a crash,
--     so ingest consumes only complete lines and leaves a trailing fragment for next
--     time.
--   * truncation is detected by comparing the offset to the live st_size: a file smaller
--     than its own watermark was replaced, and the session is deleted and re-ingested.
-- No size, mtime or head digest is stored. Size adds nothing an offset comparison does
-- not already give for an append-only file, and a head digest would catch rotate-in-
-- place, which nothing in this codebase does. If rotation is ever added the whole
-- watermark scheme needs revisiting, and the recovery is `agent reindex --rebuild`.
--
-- The summary columns ARE meta.json, moved into the database. That file exists only
-- because there was nowhere else to cache these five fields; once they are columns it
-- has no reader, and both sides of it are deleted. `alias` and `title` are extracted
-- from response *content* (Claude Code's session-naming call) and cannot be re-derived
-- without decompressing response blobs, so they must be columns. Given that the ingest
-- upsert exists for them anyway, carrying record_count / last_event_at_us / models in
-- the same statement is a few more lines and keeps the sessions list off `requests`.
CREATE TABLE sessions (
    id                INTEGER NOT NULL PRIMARY KEY,
    project_hash      TEXT    NOT NULL,
    provider          TEXT    NOT NULL,
    claude_session_id TEXT    NOT NULL,

    messages_offset   INTEGER NOT NULL DEFAULT 0,
    strings_offset    INTEGER NOT NULL DEFAULT 0,

    -- unix ns. MAX() is this table's revision counter and its ingest-freshness signal,
    -- exactly as projects.last_seen_at is for the registry.
    last_ingested_at  INTEGER NOT NULL,

    record_count      INTEGER NOT NULL DEFAULT 0,
    last_event_at_us  INTEGER,
    models            TEXT    NOT NULL DEFAULT '[]',  -- JSON array of short model names
    alias             TEXT,
    title             TEXT,

    UNIQUE (project_hash, provider, claude_session_id)
) STRICT;

-- Every distinct piece of content, addressed by the SHA-256 of its canonical bytes.
-- Roughly 3.1M message references resolve to ~118k rows; `tools` collapses to ~322.
--
-- The address for an interned string is sha256(original UTF-8), which is exactly what
-- the sidecar's StringHasher already computed and wrote into the record as "hash:<hex>".
-- The pointer in the log file is therefore already the content address, and because that
-- hash is global rather than per-session, the same string interned in two sessions
-- collapses to one row. The address for a structured value is sha256 of its canonical
-- JSON.
--
-- There is deliberately no `kind` column: the reader always knows what it is holding
-- from how it reached the blob -- a structural value via message_refs or a *_blob
-- column, a string via a pointer scan -- so a kind would be advisory only. Nor a
-- decoded-size column; length(payload) gives the stored size and zlib gives the rest.
--
-- `codec` cannot be inferred from the payload's first byte, because a raw string blob
-- may legitimately begin with zlib's 0x78, so it is explicit. Consequence: payload is
-- opaque to SQL -- no LIKE, no FTS5 over message text. The viewer filters client-side so
-- nothing pays for this today; a future server-side search needs its own index over
-- decoded text.
CREATE TABLE blobs (
    id      INTEGER NOT NULL PRIMARY KEY,
    sha256  BLOB    NOT NULL UNIQUE CHECK (length(sha256) = 32),
    codec   TEXT    NOT NULL CHECK (codec IN ('raw', 'zlib')),
    payload BLOB    NOT NULL
) STRICT;

-- One row per line of messages.jsonl -- one upstream LLM call, success or failure.
--
-- `ordinal` is the 0-based line index and is load-bearing twice over. It is the append
-- order the reader's order-key pass needs (a record with no timing inherits the last
-- start seen; keying on `start or 0` used to hoist a mid-session 429 above the
-- conversation it followed). And UNIQUE(session_id, ordinal) is the assertion that
-- ingest never misaligned -- a hard constraint with no ON CONFLICT anywhere, because a
-- collision means a watermark bug and swallowing it would hide that bug.
--
-- There is no materialized sort key: the order-key pass is a forward-fill over one
-- file's records in `ordinal` order, which is exactly what the reader has in hand, so
-- the existing function is reused at read time rather than reimplemented at ingest.
--
-- There is no byte offset or record digest. They would buy a `--verify` integrity check
-- against a source file that is still on disk and whose disagreement would be repaired
-- by re-ingesting anyway; omitting them also keeps a SHA-256 per record off the ingest
-- path.
--
-- There is no envelope blob. The request headers and litellm's own
-- request.body.metadata are ~64 MB that dedupe to nothing -- per-request content-length
-- makes them all distinct -- and no consumer reads them. Byte-exact reconstruction is
-- what the retained JSONL is for.
--
-- `error` is TEXT, not a blob reference: the callback only interns strings of 70
-- characters or more, so an error is either short or already a "hash:" pointer.
--
-- `message_refs` is little-endian uint32 blob ids in message order. Not a
-- request_message(request_id, position, blob_id) junction table, which is the obvious
-- shape and would be ~3.1M rows and ~75 MB -- ~37 MB clustered plus a mandatory ~37 MB
-- index on blob_id, without which `foreign_keys = ON` turns every DELETE FROM blobs into
-- a full scan of three million rows. Packed costs ~12 MB, no consumer wants
-- element-level addressability, and the set difference between two vectors is what makes
-- the viewer's incremental tick ~3 KB. The trade is that a dangling id is silent where a
-- foreign key would raise; that is acceptable because the database is rebuildable. The
-- repository unpacks with one array('I').frombytes() and never returns a blob id without
-- its dictionary.
CREATE TABLE requests (
    id                 INTEGER NOT NULL PRIMARY KEY,
    session_id         INTEGER NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    ordinal            INTEGER NOT NULL,

    status             TEXT    NOT NULL CHECK (status IN ('success', 'failure')),
    model              TEXT    NOT NULL,   -- verbatim; 'undefined' when absent
    usage_source       TEXT    NOT NULL CHECK (usage_source IN
                           ('native', 'standard_logging_object', 'unrecoverable')),

    -- Microseconds since the epoch. Integers rather than REAL so ordering and windowing
    -- are exact; NULL where the record genuinely carries no timestamp, which is what
    -- feeds the stats layer's "?" day key rather than a fabricated instant.
    started_at_us      INTEGER,
    first_token_at_us  INTEGER,
    ended_at_us        INTEGER,

    agent_id           TEXT,               -- x-claude-code-agent-id
    finish_reason      TEXT,               -- choices[0].finish_reason (a sibling of message)
    max_tokens         INTEGER,
    error              TEXT,

    message_refs       BLOB    NOT NULL,
    system_blob        INTEGER REFERENCES blobs (id),
    tools_blob         INTEGER REFERENCES blobs (id),
    response_blob      INTEGER REFERENCES blobs (id),

    -- Usage exactly as reported, and nothing derived from it. No cost column: pricing is
    -- recomputed on every read so pricing-table changes reach history. No day key: the
    -- stats day depends on DAY_START_HOURS, which is read from the environment at import
    -- time and can differ between two runs against the same database.
    --
    -- Cache writes are kept both ways -- the flat cache_creation_input_tokens, and the
    -- ephemeral 5m/1h split when one was available. The last-resort rule (charge an
    -- unsplit total at the 5m rate) stays in the pricing domain. On this corpus every
    -- record carrying a split reports it ONLY at
    -- usage.prompt_tokens_details.cache_creation_token_details, so that is the path
    -- ingest must read.
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_5m     INTEGER NOT NULL DEFAULT 0,
    cache_write_1h     INTEGER NOT NULL DEFAULT 0,
    cache_read         INTEGER NOT NULL DEFAULT 0,

    CHECK (cache_write_5m + cache_write_1h <= cache_write_tokens),
    UNIQUE (session_id, ordinal)
) STRICT;

-- `agent stats` in one index. This is why there is no separate metrics table: with blobs
-- out of line a `requests` row is already narrow, so the only thing a split table would
-- buy is a covering index, and an index does not need a table of its own.
--
-- PARTIAL, on exactly the population the stats fold accumulates. The predicate that
-- would otherwise decide which rows get a metrics row is the index's WHERE clause,
-- stated once instead of twice.
--
-- The leading term is an EXPRESSION, not a column. started_at_us / 3600000000 is the UTC
-- hour bucket -- the finest grain pricing needs, since each HourKey(weekday, hour) cell
-- is charged at its own rate. Because it is an expression, no calendar field is ever
-- stored: the UTC date, the weekday and the DAY_START_HOURS-shifted stats day are all
-- derived in Python from the bucket, at read time, with whatever the environment says
-- now. Materializing them as columns would freeze exactly the decision the read path is
-- supposed to keep making.
--
-- It is ordered by that bucket, so the GROUP BY needs no sorter, and it carries every
-- summed column, so the aggregate never opens the table -- which keeps the wide
-- message_refs blob off the stats path entirely. A plan assertion in the tests is what
-- catches a future column addition quietly dropping it from covering.
--
-- started_at_us appears TWICE: once bucketed as the leading ordering term, and once raw.
-- The raw copy is redundant as a key and is there to be *selected* -- MAX(started_at_us)
-- per group is the "LAST" column of the projects table, and an hour bucket is not
-- precise enough for it. A whole UTC hour maps to one local calendar date in a
-- whole-hour zone, but not in a half-hour one: at UTC+5:30 the hour 18:00-18:59 straddles
-- local midnight, so truncating to the start of the hour would report the previous day.
-- Reading the raw value off the table instead would cost a rowid seek per group and open
-- the wide row the rest of this index exists to avoid; ~5 bytes per row (~220 KB on the
-- current tree) buys the exact instant and keeps the aggregate covering.
CREATE INDEX idx_requests_usage ON requests (
    (started_at_us / 3600000000), session_id, model, usage_source,
    started_at_us,
    input_tokens, output_tokens, cache_write_tokens,
    cache_write_5m, cache_write_1h, cache_read
) WHERE status = 'success';
