# This file has been created with the assistance of an AI tool.
"""Tests for parsing the sidecar's JSONL log files into ingest chunks."""

import hashlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from agent_wrap.domain.logs.constants import MAX_CHUNK_RECORDS
from agent_wrap.domain.logs.ingest import LogFiles, RecordParser, read_ingest_chunks
from agent_wrap.infrastructure.logs.models import SessionState
from agent_wrap.lib.canonical_json import content_address

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.infrastructure.logs.models import IngestChunk


def _record(**overrides: Any) -> dict[str, Any]:
    """Build a minimal success record in the `request.body` shape."""
    rec: dict[str, Any] = {
        "timing": {"start": 1000.5, "completionStart": 1000.75, "end": 1001.25},
        "status": "success",
        "model": "bedrock/claude-sonnet-4",
        "request": {"body": {"messages": [{"role": "user", "content": "hi"}]}},
        "response": {"usage": {"input_tokens": 10, "output_tokens": 5}},
    }
    rec.update(overrides)
    return rec


def _write_session(
    session_dir: Path, records: list[dict[str, Any]], strings: list[dict[str, str]] | None = None
) -> None:
    """Write *records* and *strings* as the sidecar would, appending."""
    session_dir.mkdir(parents=True, exist_ok=True)
    with (session_dir / "messages.jsonl").open("a", encoding="utf-8") as handle:
        for rec in records:
            handle.write(json.dumps(rec) + "\n")
    if strings:
        with (session_dir / "strings.jsonl").open("a", encoding="utf-8") as handle:
            for entry in strings:
                handle.write(json.dumps(entry) + "\n")


def _drain(session_dir: Path, state: SessionState) -> list[IngestChunk]:
    return list(read_ingest_chunks(session_dir, state))


def test_a_missing_session_yields_nothing(tmp_path: Path) -> None:
    assert _drain(tmp_path / "absent", SessionState.unseen()) == []


def test_an_unchanged_session_yields_nothing(tmp_path: Path) -> None:
    """The heartbeat case: both watermarks already sit at end of file."""
    _write_session(tmp_path, [_record()])
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    resumed = SessionState(chunk.messages_offset, chunk.strings_offset, chunk.summary)
    assert _drain(tmp_path, resumed) == []


@pytest.mark.parametrize("route", ["body", "body.data"])
def test_both_request_body_shapes_are_parsed(tmp_path: Path, route: str) -> None:
    """
    57% of the real corpus nests the prompt under `request.body.data`.

    Reading only `request.body` would silently drop half the content, so both routes
    have to go through extract_record_fields.
    """
    payload = {"messages": [{"role": "user", "content": "hi"}], "tools": [{"name": "Read"}]}
    body = payload if route == "body" else {"data": payload}
    _write_session(tmp_path, [_record(request={"body": body})])

    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert len(record.message_addresses) == 1
    assert record.tools_address is not None


def test_record_fields_are_carried_through(tmp_path: Path) -> None:
    _write_session(
        tmp_path,
        [
            _record(
                request={
                    "body": {"messages": [], "max_tokens": 4096},
                    "headers": {"x-claude-code-agent-id": "agent-7"},
                },
                response={
                    "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {
                        "input_tokens": 11,
                        "output_tokens": 22,
                        "cache_creation_input_tokens": 33,
                        "cache_read_input_tokens": 44,
                    },
                },
            )
        ],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests

    assert record.ordinal == 0
    assert record.status == "success"
    assert record.model == "bedrock/claude-sonnet-4"
    assert record.usage_source == "native"
    assert record.agent_id == "agent-7"
    assert record.finish_reason == "stop"
    assert record.max_tokens == 4096
    assert (record.input_tokens, record.output_tokens) == (11, 22)
    assert (record.cache_write_tokens, record.cache_read) == (33, 44)


@pytest.mark.parametrize("cap", ["4096", True, None, {"max": 1}])
def test_a_max_tokens_that_is_not_an_integer_is_dropped(tmp_path: Path, cap: object) -> None:
    """
    A malformed cap becomes NULL rather than a number the viewer would reason from.

    ``bool`` is an ``int`` subclass, so a stray ``True`` would otherwise be stored as a
    cap of 1 -- and a cap of 1 is exactly what marks Claude Code's quota probe, which the
    viewer mutes. A real request reported that way would vanish from the session view.
    """
    _write_session(tmp_path, [_record(request={"body": {"messages": [], "max_tokens": cap}})])
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert record.max_tokens is None


def test_timings_become_exact_microseconds(tmp_path: Path) -> None:
    _write_session(tmp_path, [_record()])
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert record.started_at_us == 1_000_500_000
    assert record.first_token_at_us == 1_000_750_000
    assert record.ended_at_us == 1_001_250_000


def test_a_missing_timing_stays_null(tmp_path: Path) -> None:
    """A fabricated instant would move the record into a real day; None feeds the "?" key."""
    _write_session(
        tmp_path, [_record(timing={"start": None, "completionStart": None, "end": None})]
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert (record.started_at_us, record.first_token_at_us, record.ended_at_us) == (
        None,
        None,
        None,
    )


def test_the_cache_split_is_read_from_the_nested_path(tmp_path: Path) -> None:
    """Every record in the real tree that carries a split reports it only here."""
    _write_session(
        tmp_path,
        [
            _record(
                response={
                    "usage": {
                        "cache_creation_input_tokens": 100,
                        "prompt_tokens_details": {
                            "cache_creation_token_details": {
                                "ephemeral_5m_input_tokens": 60,
                                "ephemeral_1h_input_tokens": 40,
                            }
                        },
                    }
                }
            )
        ],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert (record.cache_write_5m, record.cache_write_1h) == (60, 40)
    assert record.cache_write_tokens == 100


def test_an_oversized_split_is_dropped_rather_than_the_record(tmp_path: Path) -> None:
    """The CHECK constraint would refuse the row, and the flat total is what is charged."""
    _write_session(
        tmp_path,
        [
            _record(
                response={
                    "usage": {
                        "cache_creation_input_tokens": 10,
                        "prompt_tokens_details": {
                            "cache_creation_token_details": {
                                "ephemeral_5m_input_tokens": 60,
                                "ephemeral_1h_input_tokens": 40,
                            }
                        },
                    }
                }
            )
        ],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert (record.cache_write_5m, record.cache_write_1h) == (0, 0)
    assert record.cache_write_tokens == 10


def test_a_failure_record_is_kept(tmp_path: Path) -> None:
    """Failures are stored; the partial index is what keeps them off the stats path."""
    _write_session(tmp_path, [_record(status="failure", error="boom")])
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    (record,) = chunk.requests
    assert record.status == "failure"
    assert record.error == "boom"


def test_identical_content_is_interned_once(tmp_path: Path) -> None:
    """
    Dedup is what keeps the work proportional.

    A conversation prefix is re-sent every turn, so two records sharing a message must
    contribute one blob and two references to it.
    """
    shared = {"role": "user", "content": "the same message"}
    _write_session(
        tmp_path,
        [
            _record(request={"body": {"messages": [shared]}}),
            _record(request={"body": {"messages": [shared, {"role": "user", "content": "new"}]}}),
        ],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())

    addresses = [blob.sha256 for blob in chunk.blobs]
    assert len(addresses) == len(set(addresses)), "a blob was emitted twice"
    assert chunk.requests[0].message_addresses[0] == chunk.requests[1].message_addresses[0]
    assert content_address(shared) in addresses


def test_blank_and_malformed_lines_are_skipped(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "messages.jsonl").write_text(
        json.dumps(_record()) + "\n\nnot json at all\n" + json.dumps(_record()) + "\n",
        encoding="utf-8",
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    assert len(chunk.requests) == 2
    # Ordinals stay contiguous, since a skipped line is not a record.
    assert [r.ordinal for r in chunk.requests] == [0, 1]


def test_a_torn_final_append_is_left_for_the_next_pass(tmp_path: Path) -> None:
    """
    The watermark must stop before an unterminated line, not parse a fragment.

    This is what makes a crash mid-append recoverable rather than corrupting.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    complete = json.dumps(_record()) + "\n"
    (tmp_path / "messages.jsonl").write_text(complete + '{"partial": ', encoding="utf-8")

    (chunk,) = _drain(tmp_path, SessionState.unseen())
    assert len(chunk.requests) == 1
    assert chunk.messages_offset == len(complete.encode("utf-8"))


def test_a_completed_append_is_picked_up_next_pass(tmp_path: Path) -> None:
    _write_session(tmp_path, [_record()])
    (first,) = _drain(tmp_path, SessionState.unseen())
    _write_session(tmp_path, [_record(model="bedrock/other")])

    resumed = SessionState(first.messages_offset, first.strings_offset, first.summary)
    (second,) = _drain(tmp_path, resumed)
    assert len(second.requests) == 1
    assert second.requests[0].ordinal == 1, "ordinals continue across passes"
    assert second.summary.record_count == 2


def test_chunks_are_bounded_by_record_count(tmp_path: Path) -> None:
    """One transaction per chunk, so a huge session must not become one huge WAL."""
    _write_session(tmp_path, [_record() for _ in range(MAX_CHUNK_RECORDS + 5)])
    chunks = _drain(tmp_path, SessionState.unseen())
    assert len(chunks) == 2
    assert len(chunks[0].requests) == MAX_CHUNK_RECORDS
    assert len(chunks[1].requests) == 5
    # Offsets advance monotonically, so resuming from either is well defined.
    assert chunks[0].messages_offset < chunks[1].messages_offset


def test_interned_strings_become_blobs_addressed_by_their_pointer(tmp_path: Path) -> None:
    original = "x" * 200
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    _write_session(
        tmp_path,
        [_record(request={"body": {"messages": [{"role": "user", "content": f"hash:{digest}"}]}})],
        strings=[{"hash": f"hash:{digest}", "original": original}],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    assert bytes.fromhex(digest) in {blob.sha256 for blob in chunk.blobs}


def test_a_string_whose_pointer_does_not_match_its_content_is_rejected(tmp_path: Path) -> None:
    """Storing content under an address nothing resolves to is worse than dropping it."""
    _write_session(
        tmp_path,
        [_record()],
        strings=[{"hash": "hash:" + "0" * 64, "original": "not what that digest addresses"}],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    assert bytes.fromhex("0" * 64) not in {blob.sha256 for blob in chunk.blobs}


def test_a_pointer_flushed_between_passes_still_resolves(tmp_path: Path) -> None:
    """
    The invariant the whole schema leans on.

    The callback flushes strings *before* appending the record that references them, so
    reading messages to offset M and only then strings to EOF cannot leave a dangling
    pointer. Here the string is already on disk while its record is not yet -- the first
    pass must take the string, so the second pass's record resolves.
    """
    original = "y" * 200
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()

    # Pass 1: the string has been flushed, its record has not been appended.
    _write_session(
        tmp_path, [_record()], strings=[{"hash": f"hash:{digest}", "original": original}]
    )
    (first,) = _drain(tmp_path, SessionState.unseen())
    assert bytes.fromhex(digest) in {blob.sha256 for blob in first.blobs}

    # Pass 2: the referencing record arrives; its pointer is already stored.
    _write_session(
        tmp_path,
        [_record(request={"body": {"messages": [{"role": "user", "content": f"hash:{digest}"}]}})],
    )
    resumed = SessionState(first.messages_offset, first.strings_offset, first.summary)
    (second,) = _drain(tmp_path, resumed)
    assert second.requests[0].message_addresses  # references it, without re-storing it
    assert bytes.fromhex(digest) not in {blob.sha256 for blob in second.blobs}


def test_summary_mirrors_the_meta_json_accumulation(tmp_path: Path) -> None:
    """
    Same rules as io._accumulate_session_meta, including last-wins rather than max.

    `models` is the short name only, sorted and deduplicated.
    """
    _write_session(
        tmp_path,
        [
            _record(model="bedrock/claude-sonnet-4", timing={"start": 1.0, "end": 5.0}),
            _record(model="anthropic/claude-opus-4", timing={"start": 2.0, "end": 3.0}),
            _record(model="bedrock/claude-sonnet-4", timing={"start": 3.0, "end": 4.0}),
        ],
    )
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    assert chunk.summary.record_count == 3
    assert chunk.summary.models == ("claude-opus-4", "claude-sonnet-4")
    # The last record's end, not the maximum -- the file is append-ordered.
    assert chunk.summary.last_event_at_us == 4_000_000


def test_truncation_is_detected_from_size_alone(tmp_path: Path) -> None:
    """A file smaller than its own watermark was replaced, not appended to."""
    _write_session(tmp_path, [_record()])
    (chunk,) = _drain(tmp_path, SessionState.unseen())
    state = SessionState(chunk.messages_offset, chunk.strings_offset, chunk.summary)
    assert not LogFiles.is_truncated(tmp_path, state)

    (tmp_path / "messages.jsonl").write_text("", encoding="utf-8")
    assert LogFiles.is_truncated(tmp_path, state)


def test_an_absent_file_is_not_truncation(tmp_path: Path) -> None:
    """A deleted directory is the walk's business, not the watermark's."""
    state = SessionState(
        messages_offset=100, strings_offset=0, summary=SessionState.unseen().summary
    )
    assert not LogFiles.is_truncated(tmp_path / "gone", state)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1.5, 1_500_000), (0, 0), (None, None), ("nope", None), (True, None)],
)
def test_to_micros_coercion(value: object, expected: int | None) -> None:
    assert RecordParser.to_micros(value) == expected
