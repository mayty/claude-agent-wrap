# This file has been created with the assistance of an AI tool.
"""
Tests for the NDJSON one open session is served as.

Every test here writes log files, ingests them, and then reads the session back through
the stream — the whole round trip, because what is under test is that the wire format
carries what the log record held. A hand-built index could agree with the stream about a
shape neither the ingester nor the browser uses.

The properties that matter are the wire *order* (content before the records that point at
it), the deduplication (one line per distinct value, however many records reference it),
and the incremental case (a client holding the first n records is sent only what is new).
"""

import hashlib
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.logs.ingest import read_ingest_chunks
from agent_wrap.domain.logs.stream import SessionStream
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.infrastructure.logs.models import SessionKey, SessionState
from agent_wrap.infrastructure.logs.repositories.requests import RequestRepository

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

    from agent_wrap.containers import Core
    from agent_wrap.infrastructure.logs.repositories.ingest import LogIngestRepository

HASH_A = "hashA"
SESSION = "sess-1"


@pytest.fixture
def pricing(mocker: MockerFixture) -> PricingService:
    """
    Return a real PricingService over a provider that prices nothing.

    Real, not a mock: ``extract_usage`` is what turns a response's usage into the token
    counts the record reports, and a canned return would assert nothing about them.
    ``compute_cost`` is the only part stubbed out, since a rate table is not the subject.
    """
    provider = mocker.Mock(spec=ProviderService)
    provider.get_provider.return_value.compute_cost.return_value = None
    return PricingService(provider_service=provider, display_service=Mock(spec=DisplayService))


@pytest.fixture
def stream(db_core: Core, pricing: PricingService) -> SessionStream:
    return SessionStream(RequestRepository(connection_factory=db_core.logs_db), pricing)


@pytest.fixture
def index(tmp_path: Path, log_ingest_repository: LogIngestRepository) -> Callable[..., None]:
    """
    Return a factory that writes one session's log files and ingests them.

    Writes into the central tree the sidecars use, since the project hash is the only
    identity the index stores. Appending to a session already written is what a second
    call with the same ids does, which is how the incremental tests grow one.
    """

    def _index(
        records: list[dict[str, Any]],
        *,
        strings: list[dict[str, str]] | None = None,
        provider: str = "litellm-bedrock",
        project_hash: str = HASH_A,
        session_id: str = SESSION,
    ) -> None:
        session_dir = tmp_path / "litellm-logs" / project_hash / provider / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        with (session_dir / "messages.jsonl").open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")
        if strings:
            with (session_dir / "strings.jsonl").open("a", encoding="utf-8") as handle:
                for entry in strings:
                    handle.write(json.dumps(entry) + "\n")
        key = SessionKey(project_hash=project_hash, provider=provider, claude_session_id=session_id)
        state = log_ingest_repository.session_state(key) or SessionState.unseen()
        for chunk in read_ingest_chunks(session_dir, state):
            log_ingest_repository.ingest_chunk(key, chunk)

    return _index


def _record(**overrides: Any) -> dict[str, Any]:
    """Build a minimal success record in the shape the sidecar writes."""
    record: dict[str, Any] = {
        "timing": {"start": 1000.5, "completionStart": 1000.75, "end": 1001.25},
        "status": "success",
        "model": "bedrock/claude-sonnet-4",
        "request": {"body": {"messages": [{"role": "user", "content": "hi"}]}},
        "response": {
            "choices": [{"message": {"role": "assistant", "content": "hello"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        "error": None,
    }
    record.update(overrides)
    return record


def _timed(start: float, **overrides: Any) -> dict[str, Any]:
    """Build a record whose whole timing is one instant, for ordering tests."""
    return _record(timing={"start": start, "completionStart": None, "end": start}, **overrides)


def _untimed(**overrides: Any) -> dict[str, Any]:
    """Build a record with an all-null timing, as every failure used to be written."""
    return _record(
        timing={"start": None, "completionStart": None, "end": None},
        status="failure",
        **overrides,
    )


def _pointer(original: str) -> tuple[str, dict[str, str]]:
    """Return the sidecar's pointer for *original* and the strings.jsonl line for it."""
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    pointer = f"hash:{digest}"
    return pointer, {"hash": pointer, "original": original}


def _read(stream: SessionStream, **kwargs: Any) -> list[dict[str, Any]]:
    """Parse the stream's lines for the one session these tests write."""
    return [
        json.loads(line)
        for line in stream.lines(
            [HASH_A], kwargs.pop("session_id", SESSION), kwargs.pop("meta", None), **kwargs
        )
    ]


def _records(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [line for line in lines if "__type__" not in line]


def _blobs(lines: list[dict[str, Any]]) -> dict[str, Any]:
    return {line["ref"]: line["value"] for line in lines if line.get("__type__") == "blob"}


def test_the_first_line_is_the_summary_the_list_already_holds(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    Passed in rather than recomputed, so the header cannot disagree with the list entry.

    The browser draws the header from this line before any record arrives.
    """
    index([_record()])
    meta = {"session_id": SESSION, "count": 1, "alias": "a-name", "title": None}

    lines = _read(stream, meta=meta)

    assert lines[0] == {"__type__": "session_meta", **meta}


def test_a_session_the_index_has_never_seen_is_a_header_and_nothing_else(
    stream: SessionStream,
) -> None:
    """Not an error: the project is real, and the session may simply not be ingested yet."""
    assert _read(stream) == [{"__type__": "session_meta"}]


def test_a_records_messages_are_references_and_the_content_arrives_before_it(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    index([_record()])

    lines = _read(stream)
    (record,) = _records(lines)

    assert record["messages"] == ["blob:1"]
    assert _blobs(lines)["blob:1"] == {"role": "user", "content": "hi"}
    # Order is the contract: a consumer resolves as it reads and never holds a line back.
    assert [line.get("__type__") for line in lines] == ["session_meta", "blob", None]


def test_a_value_two_records_share_is_sent_once(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    The whole point of the format. A conversation prefix is re-sent on every turn.

    Both records reference the same first message, so the wire carries three distinct
    values for four references.
    """
    first = {"role": "user", "content": "hi"}
    second = {"role": "assistant", "content": "hello"}
    third = {"role": "user", "content": "more"}
    index(
        [
            _timed(1.0, request={"body": {"messages": [first]}}),
            _timed(2.0, request={"body": {"messages": [first, second, third]}}),
        ]
    )

    lines = _read(stream)
    records = _records(lines)

    assert len(_blobs(lines)) == 3
    assert records[0]["messages"] == records[1]["messages"][:1]
    assert len(records[1]["messages"]) == 3


def test_the_system_prompt_and_tools_are_references_too(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    Both are identical across a whole session, so both dedupe to one line each.

    ``tools`` is ``[]`` rather than a reference when the request carried none, which is
    what keeps the client's "are there any" test reading the same as before.
    """
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "system": "be brief",
        "tools": [{"name": "Read"}],
    }
    index([_timed(1.0, request={"body": body}), _timed(2.0, request={"body": body})])

    lines = _read(stream)
    records = _records(lines)
    blobs = _blobs(lines)

    assert records[0]["system"] == records[1]["system"]
    assert records[0]["tools"] == records[1]["tools"]
    assert blobs[records[0]["system"]] == "be brief"
    assert blobs[records[0]["tools"]] == [{"name": "Read"}]


def test_a_request_with_no_system_or_tools_reports_neither(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    index([_record()])
    (record,) = _records(_read(stream))
    assert record["system"] is None
    assert record["tools"] == []


def test_the_reply_and_usage_come_off_the_stored_response(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    Inline rather than by reference: a response is unique per request, so nothing dedupes.

    ``usage`` is the response's own dict verbatim, which is also what the token counts
    beside it are computed from.
    """
    index([_record()])
    (record,) = _records(_read(stream))

    assert record["response"] == {"role": "assistant", "content": "hello"}
    assert record["usage"] == {"input_tokens": 10, "output_tokens": 5}
    assert record["context_tokens"] == 10
    assert record["output_tokens"] == 5


def test_a_failure_reports_its_error_and_no_reply(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    index([_untimed(error="429: upstream refused", response={})])
    (record,) = _records(_read(stream))

    assert record["status"] == "failure"
    assert record["error"] == "429: upstream refused"
    assert record["response"] == {}
    assert record["cost"] is None


def test_the_columns_the_viewer_reads_come_back_as_they_went_in(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """Timings are epoch seconds again, having been stored as exact microseconds."""
    index(
        [
            _record(
                request={
                    "body": {"messages": [], "max_tokens": 4096},
                    "headers": {"x-claude-code-agent-id": "agent-7"},
                },
                response={
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "length"}],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            )
        ]
    )
    (record,) = _records(_read(stream))

    assert record["timing"] == {"start": 1000.5, "completionStart": 1000.75, "end": 1001.25}
    assert record["model"] == "bedrock/claude-sonnet-4"
    assert record["agent_id"] == "agent-7"
    assert record["finish_reason"] == "length"
    assert record["max_tokens"] == 4096
    assert record["messages"] == []


def test_an_interned_string_is_sent_before_the_blob_that_quotes_it(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    A long message is a pointer inside the blob, and the original is its own line.

    The order matters for the same reason blobs precede records: the consumer resolves
    the pointer as it reads the blob, so the string has to already be in hand.
    """
    pointer, entry = _pointer("a prompt long enough that the sidecar interned it")
    index(
        [_record(request={"body": {"messages": [{"role": "user", "content": pointer}]}})],
        strings=[entry],
    )

    lines = _read(stream)
    refs = [line["ref"] for line in lines if line.get("__type__") == "blob"]

    assert refs.index(pointer) < refs.index("blob:1")
    assert _blobs(lines)[pointer] == "a prompt long enough that the sidecar interned it"
    assert _blobs(lines)["blob:1"] == {"role": "user", "content": pointer}


def test_a_pointer_in_the_error_column_is_resolved_too(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    The sidecar interns any string of 70 characters or more, an exception message included.

    The scan is over the finished record line, so a pointer anywhere in it is found --
    which is what makes ``error`` need no special case.
    """
    pointer, entry = _pointer("x" * 80)
    index([_untimed(error=pointer, response={})], strings=[entry])

    lines = _read(stream)

    assert _records(lines)[0]["error"] == pointer
    assert _blobs(lines)[pointer] == "x" * 80


def test_a_pointer_with_no_original_is_left_as_a_pointer(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    Nothing is invented for it, and nothing is blanked.

    The ingester recomputes a string's digest before storing it, so a corrupt
    ``strings.jsonl`` line leaves a pointer nothing resolves. Showing it is honest; an
    empty message would look like a message that was empty.
    """
    pointer, _entry = _pointer("never written to disk")
    index([_record(request={"body": {"messages": [{"role": "user", "content": pointer}]}})])

    lines = _read(stream)

    assert pointer not in _blobs(lines)
    assert _blobs(lines)["blob:1"] == {"role": "user", "content": pointer}


def test_a_lone_surrogate_does_not_break_the_line_it_travels_on(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    Ingest stores it with ``surrogatepass``; the wire has to be valid UTF-8 regardless.

    One unencodable character would otherwise fail the whole response rather than
    render as a replacement in one message.
    """
    index([_record(request={"body": {"messages": [{"role": "user", "content": "a\ud800b"}]}})])

    lines = _read(stream)

    # Three replacements, not one: ``surrogatepass`` encodes U+D800 as the three bytes
    # ED A0 80, and each is individually undecodable on the way back out.
    assert _blobs(lines)["blob:1"]["content"] == "a���b"


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def test_an_untimed_record_stays_where_it_was_appended(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    A timing-less failure inherits the last instant seen rather than sorting as zero.

    Keying on "start or 0" hoisted every failure to the top of the stream: the
    session-start quota probe's 429 appeared above the conversation it preceded by
    milliseconds, and a mid-session failure appeared to predate the session.
    """
    index(
        [
            _timed(1.0, model="first"),
            _untimed(model="failed-here"),
            _timed(3.0, model="last"),
        ]
    )
    assert [r["model"] for r in _records(_read(stream))] == ["first", "failed-here", "last"]


def test_a_leading_untimed_record_stays_first(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """The session-start quota probe genuinely is first, so it stays first."""
    index([_untimed(model="probe"), _timed(1.0, model="conversation")])
    assert [r["model"] for r in _records(_read(stream))] == ["probe", "conversation"]


def test_a_trailing_untimed_record_stays_last(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    index([_timed(1.0, model="conversation"), _untimed(model="ended-here")])
    assert [r["model"] for r in _records(_read(stream))] == ["conversation", "ended-here"]


def test_two_providers_merge_chronologically(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    A session that switched provider mid-flight has one directory per provider.

    Both are read and interleaved by start instant, so the chat view shows the single
    thread the user had rather than one provider's half followed by the other's.
    """
    index([_timed(1.0, model="bedrock-early"), _timed(4.0, model="bedrock-late")])
    index([_timed(2.0, model="deepseek-middle")], provider="litellm-deepseek")

    assert [r["model"] for r in _records(_read(stream))] == [
        "bedrock-early",
        "deepseek-middle",
        "bedrock-late",
    ]


# ---------------------------------------------------------------------------
# The incremental read the browser's tick uses
# ---------------------------------------------------------------------------


def test_from_index_returns_only_the_records_after_it(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    index([_timed(1.0, model="one"), _timed(2.0, model="two"), _timed(3.0, model="three")])

    lines = _read(stream, from_index=2)

    assert [r["model"] for r in _records(lines)] == ["three"]
    assert lines[0]["__type__"] == "session_meta"


def test_from_index_sends_only_the_content_the_new_records_introduced(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    What makes the one-second tick a few kilobytes instead of the whole session.

    The second record re-sends the first message and adds one, and the client already
    holds everything the first record referenced -- so only the new value travels.
    """
    first = {"role": "user", "content": "hi"}
    second = {"role": "user", "content": "and another thing"}
    index([_timed(1.0, request={"body": {"messages": [first]}})])
    index([_timed(2.0, request={"body": {"messages": [first, second]}})])

    lines = _read(stream, from_index=1)
    blobs = _blobs(lines)

    assert list(blobs.values()) == [second]
    assert len(_records(lines)[0]["messages"]) == 2


def test_from_index_beyond_the_end_returns_no_records(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """What a client sees after records were deleted under it: a header and nothing more."""
    index([_record()])
    assert _read(stream, from_index=99) == [{"__type__": "session_meta"}]


def test_limit_caps_the_records_without_hiding_the_true_length(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    A guard rather than pagination: the viewer needs the whole thread to group subagents.

    ``session_meta`` still reports the session's real length, so a capped response is
    recognisable as one.
    """
    index([_timed(1.0, model="one"), _timed(2.0, model="two"), _timed(3.0, model="three")])
    meta = {"session_id": SESSION, "count": 3}

    lines = _read(stream, meta=meta, limit=2)

    assert [r["model"] for r in _records(lines)] == ["one", "two"]
    assert lines[0]["count"] == 3


def test_from_index_and_limit_compose(stream: SessionStream, index: Callable[..., None]) -> None:
    index([_timed(1.0, model="one"), _timed(2.0, model="two"), _timed(3.0, model="three")])
    assert [r["model"] for r in _records(_read(stream, from_index=1, limit=1))] == ["two"]


def test_a_session_spanning_more_records_than_one_batch_is_whole(
    stream: SessionStream, index: Callable[..., None]
) -> None:
    """
    The batching bounds the content read, not what is served.

    Each batch excludes the blobs earlier batches already sent, so a value shared across
    the batch boundary still arrives exactly once -- and every record still arrives.
    """
    count = 150
    shared = {"role": "system", "content": "shared across every turn"}
    index(
        [
            _timed(float(i), request={"body": {"messages": [shared, {"role": "user", "n": i}]}})
            for i in range(count)
        ]
    )

    lines = _read(stream)
    blobs = _blobs(lines)

    assert len(_records(lines)) == count
    assert list(blobs.values()).count(shared) == 1
    assert len(blobs) == count + 1
