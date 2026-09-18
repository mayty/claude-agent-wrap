# This file has been edited with the assistance of an AI tool.
"""
Tests for the extraction both the ingester and the session stream go through.

``extract_record_fields`` is what is left of the old record normalizer, and it is the one
place that knows the two shapes a request body arrives in. Both readers of a record --
ingest, writing columns and blobs, and the stream, rebuilding the response side -- call
it, so a change here reaches both or neither.
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.domain.logs.normalize import extract_alias, extract_record_fields

if TYPE_CHECKING:
    from agent_wrap.domain.providers.models import LogRecord


def _epoch(iso: str) -> float:
    """ISO-8601 string -> Unix epoch seconds."""
    return datetime.fromisoformat(iso).timestamp()


def _raw_record() -> LogRecord:
    return cast(
        "LogRecord",
        {
            "timing": {
                "start": _epoch("2026-06-05T12:00:00+00:00"),
                "completionStart": None,
                "end": _epoch("2026-06-05T12:00:01+00:00"),
            },
            "status": "success",
            "model": "us.anthropic.claude-opus-4-8",
            "request": {
                "body": {
                    "data": {
                        "messages": [{"role": "user", "content": "hello"}],
                        "system": "be brief",
                        "tools": [{"name": "Read"}],
                    }
                }
            },
            "response": {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
            "error": None,
        },
    )


def test_the_request_body_is_read_through_data_when_it_has_one():
    data = extract_record_fields(_raw_record()).data
    assert data["messages"] == [{"role": "user", "content": "hello"}]
    assert data["system"] == "be brief"
    assert data["tools"] == [{"name": "Read"}]


def test_the_request_body_is_read_directly_when_it_has_no_data():
    """The other live shape: 43% of the current tree puts the body at `request.body`."""
    rec = cast(
        "LogRecord",
        {
            "timing": {"start": None, "completionStart": None, "end": None},
            "status": "success",
            "model": "m",
            "request": {
                "body": {
                    "messages": [{"role": "user", "content": "fallback body message"}],
                    "system": "fallback system",
                    "tools": [{"name": "FallbackTool"}],
                }
            },
            "response": {"choices": [{"message": {"content": "hi"}}]},
            "error": None,
        },
    )
    data = extract_record_fields(rec).data
    assert data["messages"] == [{"role": "user", "content": "fallback body message"}]
    assert data["system"] == "fallback system"
    assert data["tools"] == [{"name": "FallbackTool"}]


def test_the_reply_and_usage_come_off_the_response():
    fields = extract_record_fields(_raw_record())
    assert fields.reply == {"role": "assistant", "content": "hi"}
    assert fields.usage == {"prompt_tokens": 10, "completion_tokens": 2}


def test_a_record_with_nothing_in_it_extracts_empties_rather_than_raising():
    fields = extract_record_fields(
        cast(
            "LogRecord",
            {
                "timing": {"start": None, "completionStart": None, "end": None},
                "status": "failure",
                "model": "",
                "request": {},
                "response": {},
                "error": "boom",
            },
        )
    )
    assert fields.data == {}
    assert fields.reply == {}
    assert fields.usage == {}
    # A main-loop record (no proxy headers) carries no subagent id.
    assert fields.agent_id is None


def test_a_subagent_request_carries_its_agent_id():
    rec = _raw_record()
    rec["request"]["headers"] = {"x-claude-code-agent-id": "a27b7c3e5cb6db524"}
    assert extract_record_fields(rec).agent_id == "a27b7c3e5cb6db524"


def test_a_hash_pointer_is_extracted_verbatim():
    """
    Nothing here resolves a pointer, and nothing above it does either.

    The originals are interned in the blob store, and the browser resolves them from the
    session stream. Leaving the pointer intact is what makes that possible.
    """
    rec = _raw_record()
    rec["request"]["body"]["data"]["system"] = "hash:abc123"
    assert extract_record_fields(rec).data["system"] == "hash:abc123"


def _naming_record(content: str) -> LogRecord:
    return cast(
        "LogRecord",
        {
            "timing": {
                "start": _epoch("2026-06-05T12:00:00+00:00"),
                "completionStart": None,
                "end": _epoch("2026-06-05T12:00:01+00:00"),
            },
            "status": "success",
            "model": "m",
            "request": {},
            "response": {"choices": [{"message": {"role": "assistant", "content": content}}]},
            "error": None,
        },
    )


def test_extract_alias_from_name_payload():
    assert extract_alias(_naming_record('{"name": "agent-logs-web-viewer"}')) == (
        "agent-logs-web-viewer"
    )


def test_extract_alias_ignores_title_payload():
    assert extract_alias(_naming_record('{"title": "Build a web viewer"}')) is None


def test_extract_alias_none_for_freeform_and_missing():
    assert extract_alias(_naming_record("hi there")) is None
    assert extract_alias(_naming_record('{"name": ""}')) is None
    assert extract_alias(cast("LogRecord", {"response": {}})) is None


def _record_with_choice(choice: dict[str, Any]) -> LogRecord:
    """Build a record whose response carries *choice* as its only choice."""
    return cast(
        "LogRecord",
        {
            "timing": {"start": None, "completionStart": None, "end": None},
            "status": "success",
            "model": "m",
            "request": {},
            "response": {"choices": [choice]},
            "error": None,
        },
    )


def test_the_finish_reason_is_extracted():
    rec = _record_with_choice(
        {
            "message": {"role": "assistant", "content": "cut off —"},
            "finish_reason": "content_filter",
        }
    )
    assert extract_record_fields(rec).finish_reason == "content_filter"


def test_the_finish_reason_survives_a_missing_message():
    """The reason is a sibling of `message`, so it survives `message` being absent."""
    fields = extract_record_fields(_record_with_choice({"finish_reason": "length"}))
    assert fields.finish_reason == "length"
    assert fields.reply == {}


def test_the_finish_reason_is_none_when_absent():
    rec = _record_with_choice({"message": {"role": "assistant", "content": "hi"}})
    assert extract_record_fields(rec).finish_reason is None
    assert extract_record_fields(_raw_record()).finish_reason is None


def test_the_finish_reason_is_none_without_choices_or_response():
    no_choices = cast("LogRecord", {"response": {"usage": {}}})
    assert extract_record_fields(no_choices).finish_reason is None
    assert extract_record_fields(cast("LogRecord", {})).finish_reason is None


def test_a_non_string_finish_reason_is_dropped():
    """A malformed reason is dropped rather than handed to the viewer to render."""
    rec = _record_with_choice({"message": {"content": "hi"}, "finish_reason": {"why": "stop"}})
    assert extract_record_fields(rec).finish_reason is None
