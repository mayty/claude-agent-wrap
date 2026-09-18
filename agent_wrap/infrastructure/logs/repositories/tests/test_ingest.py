# This file has been created with the assistance of an AI tool.
"""Tests for the logs database's write side."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from agent_wrap.constants import HASH_POINTER_PREFIX
from agent_wrap.exceptions import StorageError
from agent_wrap.infrastructure.logs.constants import CODEC_RAW, CODEC_ZLIB, ZLIB_MIN_BYTES
from agent_wrap.infrastructure.logs.models import (
    BlobSweep,
    ContentBlob,
    IngestChunk,
    RequestRecord,
    SessionKey,
    SessionState,
    SessionSummary,
)
from agent_wrap.infrastructure.logs.repositories.ingest import BlobCodec, LogIngestRepository

if TYPE_CHECKING:
    from agent_wrap.containers import Core
    from agent_wrap.infrastructure.logs.repositories.sessions import SessionRepository

KEY = SessionKey(project_hash="hash0", provider="litellm-bedrock", claude_session_id="sess-a")
OTHER_KEY = SessionKey(project_hash="hash1", provider="litellm-bedrock", claude_session_id="sess-b")

EMPTY_SUMMARY = SessionSummary(
    record_count=0, last_event_at_us=None, models=(), alias=None, title=None
)


def _blob(payload: bytes) -> ContentBlob:
    """Build a raw-codec blob addressed by its own content."""
    return ContentBlob(sha256=hashlib.sha256(payload).digest(), codec=CODEC_RAW, payload=payload)


def _request(ordinal: int = 0, **overrides: Any) -> RequestRecord:
    fields: dict[str, Any] = {
        "ordinal": ordinal,
        "status": "success",
        "model": "claude-sonnet-4",
        "usage_source": "native",
        "started_at_us": 1_000_000,
        "first_token_at_us": None,
        "ended_at_us": 2_000_000,
        "agent_id": None,
        "finish_reason": None,
        "max_tokens": None,
        "error": None,
        "message_addresses": (),
        "system_address": None,
        "tools_address": None,
        "response_address": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_write_tokens": 0,
        "cache_write_5m": 0,
        "cache_write_1h": 0,
        "cache_read": 0,
    }
    fields.update(overrides)
    return RequestRecord(**fields)


def _chunk(
    *,
    blobs: tuple[ContentBlob, ...] = (),
    requests: tuple[RequestRecord, ...] = (),
    summary: SessionSummary | None = None,
    messages_offset: int = 100,
    strings_offset: int = 0,
) -> IngestChunk:
    return IngestChunk(
        blobs=blobs,
        requests=requests,
        summary=summary or EMPTY_SUMMARY._replace(record_count=len(requests)),
        messages_offset=messages_offset,
        strings_offset=strings_offset,
    )


def test_an_unseen_session_has_no_state(log_ingest_repository: LogIngestRepository) -> None:
    """``None`` rather than a zero state, so a caller can tell "never seen" from "empty"."""
    assert log_ingest_repository.session_state(KEY) is None


def test_ingesting_a_chunk_stores_its_watermarks(
    log_ingest_repository: LogIngestRepository,
) -> None:
    log_ingest_repository.ingest_chunk(
        KEY, _chunk(requests=(_request(),), messages_offset=512, strings_offset=64)
    )
    state = log_ingest_repository.session_state(KEY)
    assert state is not None
    assert (state.messages_offset, state.strings_offset) == (512, 64)


def test_the_session_summary_round_trips(log_ingest_repository: LogIngestRepository) -> None:
    summary = SessionSummary(
        record_count=3,
        last_event_at_us=99,
        models=("claude-opus-4", "claude-sonnet-4"),
        alias="my-session",
        title="A title",
    )
    log_ingest_repository.ingest_chunk(KEY, _chunk(summary=summary))
    state = log_ingest_repository.session_state(KEY)
    assert state is not None
    assert state.summary == summary


def test_a_later_chunk_does_not_erase_an_alias_it_lacks(
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    Claude Code's naming call is one record in the session.

    Every subsequent chunk carries `alias=None`, and assigning that would wipe the name
    the viewer shows -- hence COALESCE in the upsert.
    """
    named = EMPTY_SUMMARY._replace(alias="my-session", title="A title")
    log_ingest_repository.ingest_chunk(KEY, _chunk(summary=named, messages_offset=10))
    log_ingest_repository.ingest_chunk(KEY, _chunk(summary=EMPTY_SUMMARY, messages_offset=20))

    state = log_ingest_repository.session_state(KEY)
    assert state is not None
    assert (state.summary.alias, state.summary.title) == ("my-session", "A title")
    assert state.messages_offset == 20, "the rest of the summary still advances"


def test_re_observed_content_is_stored_once(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """The blob store is a set; a conflict is the normal case, not an error."""
    blob = _blob(b"shared content")
    log_ingest_repository.ingest_chunk(KEY, _chunk(blobs=(blob,), messages_offset=10))
    log_ingest_repository.ingest_chunk(KEY, _chunk(blobs=(blob,), messages_offset=20))

    with db_core.logs_db.ro() as connection:
        total = connection.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()["n"]
    assert total == 1


def test_message_references_round_trip_through_the_packed_vector(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """Order and repetition both matter: it is the message order the viewer renders."""
    first, second = _blob(b"first"), _blob(b"second")
    record = _request(
        message_addresses=(first.sha256, second.sha256, first.sha256),
    )
    log_ingest_repository.ingest_chunk(KEY, _chunk(blobs=(first, second), requests=(record,)))

    with db_core.logs_db.ro() as connection:
        row = connection.execute(
            "SELECT r.message_refs, b.id AS first_id FROM requests r, blobs b WHERE b.sha256 = ?",
            (first.sha256,),
        ).fetchone()
    ids = BlobCodec.unpack_ids(row["message_refs"])
    assert len(ids) == 3
    assert ids[0] == ids[2] == row["first_id"]
    assert ids[1] != ids[0]


def test_a_chunk_may_reference_a_blob_an_earlier_chunk_carried(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """
    Content is deduplicated across a whole session pass, but chunks are separate writes.

    So a later chunk routinely points at a blob an earlier one already committed and
    does not carry it again -- which is every session large enough to be chunked. This
    failed against the real log tree while every single-chunk test passed.
    """
    shared = _blob(b"a conversation prefix, re-sent every turn")
    log_ingest_repository.ingest_chunk(
        KEY,
        _chunk(
            blobs=(shared,),
            requests=(_request(ordinal=0, message_addresses=(shared.sha256,)),),
            messages_offset=10,
        ),
    )
    # Second chunk carries no blobs at all, but still references the first one's.
    log_ingest_repository.ingest_chunk(
        KEY,
        _chunk(
            requests=(_request(ordinal=1, message_addresses=(shared.sha256,)),),
            messages_offset=20,
        ),
    )

    with db_core.logs_db.ro() as connection:
        rows = connection.execute("SELECT message_refs FROM requests ORDER BY ordinal").fetchall()
        blob_count = connection.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()["n"]
    assert blob_count == 1
    assert BlobCodec.unpack_ids(rows[0]["message_refs"]) == BlobCodec.unpack_ids(
        rows[1]["message_refs"]
    )


def test_optional_blob_columns_stay_null_when_absent(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(),)))
    with db_core.logs_db.ro() as connection:
        row = connection.execute(
            "SELECT system_blob, tools_blob, response_blob FROM requests"
        ).fetchone()
    assert (row["system_blob"], row["tools_blob"], row["response_blob"]) == (None, None, None)


def test_a_duplicate_ordinal_surfaces_as_a_storage_error(
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    A collision means ingest misaligned against its watermark.

    `match` matters: WritesNotEnabledError is a StorageError too, so a bare raises()
    would also pass on a missing grant.
    """
    log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(ordinal=0),)))
    with pytest.raises(StorageError, match="write failed"):
        log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(ordinal=0),)))


def test_the_same_ordinal_in_another_session_is_fine(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """The constraint is per session; ordinals are line numbers, not global ids."""
    log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(ordinal=0),)))
    log_ingest_repository.ingest_chunk(OTHER_KEY, _chunk(requests=(_request(ordinal=0),)))

    with db_core.logs_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"] == 2


def test_a_failed_chunk_leaves_the_watermarks_where_they_were(
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    The whole chunk is one transaction, which is what makes resume correct.

    If a rejected row could advance the offset, the next pass would skip real content.
    """
    log_ingest_repository.ingest_chunk(
        KEY, _chunk(requests=(_request(ordinal=0),), messages_offset=100)
    )
    with pytest.raises(StorageError, match="write failed"):
        log_ingest_repository.ingest_chunk(
            KEY, _chunk(requests=(_request(ordinal=0),), messages_offset=999)
        )

    state = log_ingest_repository.session_state(KEY)
    assert state is not None
    assert state.messages_offset == 100


def test_reset_session_forgets_it_entirely(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(),)))
    log_ingest_repository.reset_session(KEY)

    assert log_ingest_repository.session_state(KEY) is None
    with db_core.logs_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"] == 0


def test_reset_session_keeps_the_blobs(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """Content-addressed, so the re-ingest re-uses them; reclaiming is the sweep's job."""
    log_ingest_repository.ingest_chunk(KEY, _chunk(blobs=(_blob(b"kept"),)))
    log_ingest_repository.reset_session(KEY)
    with db_core.logs_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM blobs").fetchone()["n"] == 1


def test_delete_projects_removes_only_the_named_hashes(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(),)))
    log_ingest_repository.ingest_chunk(OTHER_KEY, _chunk(requests=(_request(),)))

    log_ingest_repository.delete_projects([KEY.project_hash])

    assert log_ingest_repository.session_state(KEY) is None
    assert log_ingest_repository.session_state(OTHER_KEY) is not None
    with db_core.logs_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"] == 1


def test_delete_projects_with_nothing_to_do_opens_no_transaction(
    read_only_core: Core,
) -> None:
    """An empty call must not need a write grant, so a no-op cleanup cannot fail."""
    repository = LogIngestRepository(connection_factory=read_only_core.logs_db)
    repository.delete_projects([])  # no StorageError


def _dated_chunk(last_event_at_us: int | None, *, messages_offset: int = 100) -> IngestChunk:
    """Build a one-request chunk whose session carries *last_event_at_us*."""
    return _chunk(
        requests=(_request(),),
        summary=EMPTY_SUMMARY._replace(record_count=1, last_event_at_us=last_event_at_us),
        messages_offset=messages_offset,
    )


def test_expired_sessions_selects_by_the_last_event_not_the_ingest_time(
    log_ingest_repository: LogIngestRepository,
) -> None:
    """
    Age is a property of the requests, never of when this host happened to read them.

    ``last_ingested_at`` is stamped with *now* by the upsert both of these get, so a
    query that used it would return either both sessions or neither.
    """
    log_ingest_repository.ingest_chunk(KEY, _dated_chunk(1_000_000, messages_offset=100))
    log_ingest_repository.ingest_chunk(OTHER_KEY, _dated_chunk(9_000_000))

    expired = log_ingest_repository.expired_sessions(5_000_000)

    assert [watermark.key for watermark in expired] == [KEY]
    assert expired[0].messages_offset == 100


def test_expired_sessions_never_returns_an_undated_session(
    log_ingest_repository: LogIngestRepository,
) -> None:
    """No instant to compare means no age, and the caller deletes files on this answer."""
    log_ingest_repository.ingest_chunk(KEY, _dated_chunk(None))

    assert log_ingest_repository.expired_sessions(2**62) == []


def test_delete_sessions_removes_only_the_named_sessions(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """
    Per session, which is what separates retention from forgetting a whole project.

    Both sessions here belong to different projects, but the point is the granularity:
    the delete is keyed on the triple, so a project keeps every session retention did
    not name.
    """
    log_ingest_repository.ingest_chunk(KEY, _chunk(requests=(_request(),)))
    log_ingest_repository.ingest_chunk(OTHER_KEY, _chunk(requests=(_request(),)))

    log_ingest_repository.delete_sessions([KEY])

    assert log_ingest_repository.session_state(KEY) is None
    assert log_ingest_repository.session_state(OTHER_KEY) is not None
    with db_core.logs_db.ro() as connection:
        assert connection.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"] == 1


def test_delete_sessions_with_nothing_to_do_opens_no_transaction(read_only_core: Core) -> None:
    """Retention that found nothing must not need a write grant it was never given."""
    repository = LogIngestRepository(connection_factory=read_only_core.logs_db)
    repository.delete_sessions([])  # no StorageError


def test_ingesting_an_empty_chunk_still_advances_the_watermarks(
    log_ingest_repository: LogIngestRepository,
) -> None:
    """A pass that only picked up interned strings must not be replayed forever."""
    log_ingest_repository.ingest_chunk(KEY, _chunk(messages_offset=0, strings_offset=42))
    state = log_ingest_repository.session_state(KEY)
    assert state is not None
    assert state.strings_offset == 42


@pytest.mark.parametrize(
    ("raw", "expected_codec"),
    [
        (b"", CODEC_RAW),
        (b"x" * (ZLIB_MIN_BYTES - 1), CODEC_RAW),
        (b"x" * ZLIB_MIN_BYTES, CODEC_ZLIB),
    ],
)
def test_the_codec_is_chosen_by_size(raw: bytes, expected_codec: str) -> None:
    codec, payload = BlobCodec.encode(raw)
    assert codec == expected_codec
    assert BlobCodec.decode(codec, payload) == raw


def test_a_payload_beginning_with_the_zlib_magic_byte_is_not_misread() -> None:
    """
    The codec is stored rather than sniffed for exactly this case.

    A raw payload may legitimately start with 0x78, and there would be no way to tell
    it from a compressed one.
    """
    raw = b"\x78\x9c" + b"not actually compressed"
    codec, payload = BlobCodec.encode(raw)
    assert codec == CODEC_RAW
    assert BlobCodec.decode(codec, payload) == raw


@pytest.mark.parametrize("ids", [(), (1,), (1, 2, 3), (7, 7, 7), (4_294_967_295,)])
def test_blob_id_vectors_round_trip(ids: tuple[int, ...]) -> None:
    assert BlobCodec.unpack_ids(BlobCodec.pack_ids(ids)) == ids


def test_packed_vectors_are_little_endian_regardless_of_host() -> None:
    """The database file is portable; `array` is native-endian, so the order is forced."""
    assert BlobCodec.pack_ids([1, 2]) == b"\x01\x00\x00\x00\x02\x00\x00\x00"


def test_unseen_state_is_a_usable_starting_point() -> None:
    state = SessionState.unseen()
    assert (state.messages_offset, state.strings_offset) == (0, 0)
    assert state.summary.record_count == 0


def _pointer(original: bytes) -> str:
    """Build the ``hash:`` pointer that reaches the blob storing *original*."""
    return HASH_POINTER_PREFIX + hashlib.sha256(original).hexdigest()


def _stored_addresses(core: Core) -> set[bytes]:
    with core.logs_db.ro() as connection:
        return {row["sha256"] for row in connection.execute("SELECT sha256 FROM blobs")}


def test_a_blob_no_request_reaches_is_swept(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """The whole point: content left behind by a truncation re-ingest goes."""
    log_ingest_repository.ingest_chunk(KEY, _chunk(blobs=(_blob(b"orphaned content"),)))

    sweep = log_ingest_repository.sweep_blobs()

    assert sweep.removed == 1
    assert sweep.freed_bytes == len(b"orphaned content")
    assert _stored_addresses(db_core) == set()


def test_a_blob_a_message_vector_names_survives(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    kept = _blob(b"a message")
    log_ingest_repository.ingest_chunk(
        KEY,
        _chunk(
            blobs=(kept, _blob(b"orphaned")),
            requests=(_request(message_addresses=(kept.sha256,)),),
        ),
    )

    assert log_ingest_repository.sweep_blobs().removed == 1
    assert _stored_addresses(db_core) == {kept.sha256}


@pytest.mark.parametrize("column", ["system_address", "tools_address", "response_address"])
def test_a_blob_one_of_the_three_columns_names_survives(
    log_ingest_repository: LogIngestRepository, db_core: Core, column: str
) -> None:
    kept = _blob(b"named by a column")
    overrides: dict[str, Any] = {column: kept.sha256}
    log_ingest_repository.ingest_chunk(
        KEY, _chunk(blobs=(kept,), requests=(_request(**overrides),))
    )

    assert log_ingest_repository.sweep_blobs().removed == 0
    assert _stored_addresses(db_core) == {kept.sha256}


def test_a_blob_only_a_pointer_inside_another_blob_reaches_survives(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """
    The case the sweep exists to get right, and the one nothing structural protects.

    An interned string is never named by a request. It is reached only by the 69
    characters a message's canonical JSON quotes it with -- so a sweep that looked at
    ids alone would delete most of the text in the index.
    """
    original = b"x" * 200
    interned = _blob(original)
    quoting = _blob(json.dumps({"content": _pointer(original)}).encode())
    log_ingest_repository.ingest_chunk(
        KEY,
        _chunk(
            blobs=(interned, quoting),
            requests=(_request(message_addresses=(quoting.sha256,)),),
        ),
    )

    assert log_ingest_repository.sweep_blobs().removed == 0
    assert _stored_addresses(db_core) == {interned.sha256, quoting.sha256}


def test_the_pointer_scan_follows_a_pointer_inside_an_interned_string(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """
    An interned original is arbitrary text and may quote a pointer of its own.

    Two rounds, not one: the middle string is reached from the message, and only
    scanning *it* reaches the third. One round would delete the innermost.
    """
    inner = b"y" * 200
    middle = json.dumps(_pointer(inner)).encode() + b"z" * 200
    interned_inner = _blob(inner)
    interned_middle = _blob(middle)
    quoting = _blob(json.dumps({"content": _pointer(middle)}).encode())
    log_ingest_repository.ingest_chunk(
        KEY,
        _chunk(
            blobs=(interned_inner, interned_middle, quoting),
            requests=(_request(message_addresses=(quoting.sha256,)),),
        ),
    )

    assert log_ingest_repository.sweep_blobs().removed == 0
    assert _stored_addresses(db_core) == {
        interned_inner.sha256,
        interned_middle.sha256,
        quoting.sha256,
    }


def test_a_blob_only_the_error_column_reaches_survives(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """
    A failure's message is a pointer in a TEXT column, not a blob reference.

    Nothing about it is structural, and the reader resolves it exactly like any other
    pointer -- so a sweep that scanned only payloads would delete the message of every
    failure long enough to have been interned.
    """
    original = b"429 rate_limit_error: " + b"detail " * 40
    interned = _blob(original)
    log_ingest_repository.ingest_chunk(
        KEY,
        _chunk(
            blobs=(interned,),
            requests=(_request(status="failure", error=_pointer(original)),),
        ),
    )

    assert log_ingest_repository.sweep_blobs().removed == 0
    assert _stored_addresses(db_core) == {interned.sha256}


def test_a_deleted_project_takes_its_content_with_it(
    log_ingest_repository: LogIngestRepository, db_core: Core
) -> None:
    """The two halves of `agent cleanup`, in the order the verb runs them."""
    kept = _blob(b"still referenced")
    doomed = _blob(b"only that project's")
    log_ingest_repository.ingest_chunk(
        KEY, _chunk(blobs=(doomed,), requests=(_request(message_addresses=(doomed.sha256,)),))
    )
    log_ingest_repository.ingest_chunk(
        OTHER_KEY, _chunk(blobs=(kept,), requests=(_request(message_addresses=(kept.sha256,)),))
    )

    log_ingest_repository.delete_projects([KEY.project_hash])

    assert log_ingest_repository.sweep_blobs().removed == 1
    assert _stored_addresses(db_core) == {kept.sha256}


def test_a_sweep_with_nothing_to_do_opens_no_transaction(read_only_core: Core) -> None:
    """An empty store needs no write grant, so a no-op cleanup cannot fail."""
    repository = LogIngestRepository(connection_factory=read_only_core.logs_db)
    assert repository.sweep_blobs() == BlobSweep(removed=0, freed_bytes=0)


def test_a_sweep_returns_the_pages_to_the_database_file(
    log_ingest_repository: LogIngestRepository, log_session_repository: SessionRepository
) -> None:
    """
    The incremental vacuum, which is the only reason `auto_vacuum` is set.

    Without it the pages become a free list and the file never shrinks -- and the
    pragma frees exactly one page unless it is stepped, which no error reports.
    """
    log_ingest_repository.ingest_chunk(
        KEY, _chunk(blobs=tuple(_blob(f"orphan {n}".encode() + b"x" * 20_000) for n in range(40)))
    )
    before = log_session_repository.footprint().database_bytes

    assert log_ingest_repository.sweep_blobs().removed == 40
    assert log_session_repository.footprint().database_bytes < before
