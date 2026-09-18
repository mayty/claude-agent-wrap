# This file has been created with the assistance of an AI tool.
"""
Tests for the content read side: what one open session is served from.

Rows go in through the ingest repository rather than by hand, because the two halves
have to agree about the same bytes — a packed reference vector written one way and
unpacked another would still round-trip through a hand-written row. What is *not* under
test here is the wire format or the order the viewer shows records in: both are read-time
policy and live in ``domain/logs/stream.py``.
"""

import hashlib
import json
import zlib
from typing import TYPE_CHECKING

import pytest

from agent_wrap.infrastructure.logs.models import (
    ContentBlob,
    IngestChunk,
    RequestRecord,
    SessionKey,
    SessionSummary,
)
from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository

if TYPE_CHECKING:
    from agent_wrap.containers import Core
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository

KEY = SessionKey(project_hash="hashA", provider="litellm-bedrock", claude_session_id="sess-1")


@pytest.fixture
def requests_repo(db_core: Core) -> RequestRepository:
    return RequestRepository(connection_factory=db_core.logs_db)


def _blob(value: object) -> ContentBlob:
    """Intern one structural value the way the parser does, uncompressed."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return ContentBlob(sha256=hashlib.sha256(raw).digest(), codec="raw", payload=raw)


def _string_blob(original: str) -> ContentBlob:
    """Intern one already-hashed string, addressed by its own pointer as ingest does."""
    raw = original.encode("utf-8")
    return ContentBlob(sha256=hashlib.sha256(raw).digest(), codec="raw", payload=raw)


def _record(ordinal: int, **overrides: object) -> RequestRecord:
    fields: dict[str, object] = {
        "ordinal": ordinal,
        "status": "success",
        "model": "bedrock/claude-sonnet-4",
        "usage_source": "native",
        "started_at_us": 1_000_000 + ordinal,
        "first_token_at_us": None,
        "ended_at_us": None,
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
    return RequestRecord(**fields)  # pyrefly: ignore [bad-argument-type]


def _ingest(
    repo: LogIngestRepository,
    records: tuple[RequestRecord, ...],
    blobs: tuple[ContentBlob, ...] = (),
    *,
    key: SessionKey = KEY,
) -> None:
    repo.ingest_chunk(
        key,
        IngestChunk(
            blobs=blobs,
            requests=records,
            summary=SessionSummary(
                record_count=len(records),
                last_event_at_us=None,
                models=("claude-sonnet-4",),
                alias=None,
                title=None,
            ),
            messages_offset=100,
            strings_offset=0,
        ),
    )


def test_a_session_is_found_by_any_of_its_projects_hashes(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    One query answers for a whole group, which may be thousands of member projects.

    The id and the record count come back with it: the id is what ``requests`` is keyed
    by, and nothing above the storage layer can read content without it.
    """
    _ingest(log_ingest_repository, (_record(0), _record(1)))

    found = requests_repo.sessions_for(["hashZ", "hashA"], "sess-1")

    assert [session.key for session in found] == [KEY]
    assert found[0].record_count == 2
    assert found[0].session_id > 0


def test_a_session_that_spans_two_directories_comes_back_sorted(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    A provider switch mid-session leaves one row per directory, and both are returned.

    Sorted by key rather than in insertion order, so the merge the domain performs on
    top of this is reproducible -- the tree's own order is ``iterdir()``'s, which is not.
    """
    second = KEY._replace(provider="litellm-anthropic")
    _ingest(log_ingest_repository, (_record(0),), key=second)
    _ingest(log_ingest_repository, (_record(0),))

    found = requests_repo.sessions_for(["hashA"], "sess-1")

    assert [session.key.provider for session in found] == [
        "litellm-anthropic",
        "litellm-bedrock",
    ]


def test_an_unknown_session_or_hash_finds_nothing(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    _ingest(log_ingest_repository, (_record(0),))
    assert requests_repo.sessions_for(["hashA"], "other-session") == []
    assert requests_repo.sessions_for(["hashB"], "sess-1") == []


def test_a_project_with_no_hashes_asks_nothing(requests_repo: RequestRepository) -> None:
    """
    A project whose logs dir resolves outside the shared tree owns no hash.

    It is a real project the viewer lists, so this has to be an empty answer rather than
    an error -- and it must not become an ``IN ()`` that the database has to parse.
    """
    assert requests_repo.sessions_for([], "sess-1") == []


def test_start_instants_come_back_in_ordinal_order(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    Position in the list is the ordinal, and a record with no timing stays None.

    Turning a missing instant into zero here would sort every failure to the top of the
    session, which is the bug the domain's forward-fill exists to avoid.
    """
    _ingest(
        log_ingest_repository,
        (
            _record(0, started_at_us=7_000),
            _record(1, started_at_us=None),
            _record(2, started_at_us=5_000),
        ),
    )
    (session,) = requests_repo.sessions_for(["hashA"], "sess-1")

    assert requests_repo.start_instants(session.session_id) == [7_000, None, 5_000]


def test_requests_come_back_keyed_by_ordinal_whatever_order_they_were_asked_in(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    _ingest(log_ingest_repository, (_record(0), _record(1), _record(2)))
    (session,) = requests_repo.sessions_for(["hashA"], "sess-1")

    found = requests_repo.requests(session.session_id, [2, 0])

    assert sorted(found) == [0, 2]
    assert found[2].ordinal == 2
    assert found[0].started_at_us == 1_000_000


def test_asking_for_no_ordinals_touches_the_database_not_at_all(
    requests_repo: RequestRepository,
) -> None:
    assert requests_repo.requests(1, []) == {}
    assert requests_repo.blob_texts([]) == {}
    assert requests_repo.string_texts([]) == {}


def test_a_records_content_comes_back_as_blob_ids_in_message_order(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    The packed vector round-trips, repeats included -- it is the order the viewer renders.

    A conversation prefix is re-sent on every turn, so the same id appearing twice in one
    record is the normal case rather than a corruption.
    """
    first = _blob({"role": "user", "content": "hi"})
    second = _blob({"role": "assistant", "content": "hello"})
    system = _blob("be brief")
    _ingest(
        log_ingest_repository,
        (
            _record(
                0,
                message_addresses=(first.sha256, second.sha256, first.sha256),
                system_address=system.sha256,
            ),
        ),
        (first, second, system),
    )
    (session,) = requests_repo.sessions_for(["hashA"], "sess-1")
    (row,) = requests_repo.requests(session.session_id, [0]).values()

    assert len(row.message_blobs) == 3
    assert row.message_blobs[0] == row.message_blobs[2]
    assert row.message_blobs[0] != row.message_blobs[1]
    assert row.system_blob not in row.message_blobs
    assert row.tools_blob is None
    assert row.response_blob is None

    system_blob = row.system_blob
    assert system_blob is not None
    texts = requests_repo.blob_texts([*row.message_blobs, system_blob])
    assert json.loads(texts[row.message_blobs[0]]) == {"role": "user", "content": "hi"}
    assert texts[system_blob] == '"be brief"'


def test_a_compressed_blob_is_decoded_on_the_way_out(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """The codec is the storage layer's business, so nothing above it ever sees one."""
    value = {"role": "user", "content": "x" * 4096}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    compressed = ContentBlob(
        sha256=hashlib.sha256(raw).digest(), codec="zlib", payload=zlib.compress(raw)
    )
    _ingest(
        log_ingest_repository,
        (_record(0, message_addresses=(compressed.sha256,)),),
        (compressed,),
    )
    (session,) = requests_repo.sessions_for(["hashA"], "sess-1")
    (row,) = requests_repo.requests(session.session_id, [0]).values()

    assert json.loads(requests_repo.blob_texts(row.message_blobs)[row.message_blobs[0]]) == value


def test_an_interned_string_is_found_by_its_pointers_address(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    The 64 hex characters of a ``hash:`` pointer *are* the row's address.

    That is what makes the same string interned in two sessions one row, and what lets
    the reader resolve a pointer it found inside a blob with no extra bookkeeping.
    """
    interned = _string_blob("a very long original string")
    _ingest(log_ingest_repository, (_record(0),), (interned,))

    found = requests_repo.string_texts([interned.sha256])

    assert found == {interned.sha256: "a very long original string"}


def test_an_address_with_no_row_is_absent_rather_than_empty(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    A pointer whose original never reached the index has no content to serve.

    Absent rather than an empty string, so the reader can tell the two apart and show
    the pointer instead of blanking the message.
    """
    interned = _string_blob("present")
    _ingest(log_ingest_repository, (_record(0),), (interned,))

    found = requests_repo.string_texts([interned.sha256, bytes(32)])

    assert list(found) == [interned.sha256]


def test_a_lone_surrogate_survives_as_replacement_text(
    requests_repo: RequestRepository, log_ingest_repository: LogIngestRepository
) -> None:
    """
    Ingest encodes with ``surrogatepass``, so a blob's bytes are not always valid UTF-8.

    Every consumer of this text puts it on a UTF-8 wire, where a surviving surrogate
    would fail the whole response rather than one message -- so it is replaced here.
    """
    raw = "before\ud800after".encode("utf-8", errors="surrogatepass")
    blob = ContentBlob(sha256=hashlib.sha256(raw).digest(), codec="raw", payload=raw)
    _ingest(log_ingest_repository, (_record(0),), (blob,))

    text = requests_repo.string_texts([blob.sha256])[blob.sha256]

    assert text.startswith("before")
    assert text.endswith("after")
    assert text.encode("utf-8")  # would raise on a surviving surrogate
