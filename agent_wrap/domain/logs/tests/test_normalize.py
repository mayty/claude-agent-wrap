# This file has been edited with the assistance of an AI tool.
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from agent_wrap.domain.logs.normalize import (
    extract_alias,
    normalize_record,
    normalize_record_unresolved,
)

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


def test_normalize_pulls_real_request_data():
    out = normalize_record(_raw_record(), {})
    assert out["messages"] == [{"role": "user", "content": "hello"}]
    assert out["system"] == "be brief"
    assert out["tools"] == [{"name": "Read"}]


def test_normalize_falls_back_to_body_when_data_is_not_dict():
    # Simulate a record where request.body exists but lacks a "data" dict,
    # so the normalization falls back to using "body" directly for messages/system/tools.
    rec = cast(
        "LogRecord",
        {
            "timing": {
                "start": _epoch("2026-06-05T12:00:00+00:00"),
                "completionStart": None,
                "end": _epoch("2026-06-05T12:00:01+00:00"),
            },
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
    out = normalize_record(rec, {})
    assert out["messages"] == [{"role": "user", "content": "fallback body message"}]
    assert out["system"] == "fallback system"
    assert out["tools"] == [{"name": "FallbackTool"}]


def test_normalize_extracts_response_and_usage():
    out = normalize_record(_raw_record(), {})
    assert out["response"] == {"role": "assistant", "content": "hi"}
    assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 2}


def test_normalize_passes_through_timing():
    out = normalize_record(_raw_record(), {})
    assert out["timing"] == {
        "start": _epoch("2026-06-05T12:00:00+00:00"),
        "completionStart": None,
        "end": _epoch("2026-06-05T12:00:01+00:00"),
    }


def test_normalize_resolves_hashes():
    rec = _raw_record()
    rec["request"]["body"]["data"]["system"] = "hash:s"
    out = normalize_record(rec, {"hash:s": "resolved system"})
    assert out["system"] == "resolved system"


def test_normalize_tolerates_missing_pieces():
    out = normalize_record(
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
        ),
        {},
    )
    assert out["messages"] == []
    assert out["tools"] == []
    assert out["response"] == {}
    assert out["usage"] == {}
    assert out["error"] == "boom"
    # A main-loop record (no proxy headers) carries no subagent id.
    assert out["agent_id"] is None


def test_normalize_extracts_subagent_agent_id():
    rec = _raw_record()
    rec["request"]["headers"] = {
        "x-claude-code-agent-id": "a27b7c3e5cb6db524",
    }
    out = normalize_record(rec, {})
    assert out["agent_id"] == "a27b7c3e5cb6db524"


def test_normalize_agent_id_none_without_header():
    # Main-loop requests have no x-claude-code-agent-id header.
    out = normalize_record(_raw_record(), {})
    assert out["agent_id"] is None


def test_normalize_record_unresolved_keeps_hashes():
    """normalize_record_unresolved preserves hash: references without resolving."""
    rec = {
        "timing": {"start": 1.0, "completionStart": None, "end": 2.0},
        "status": "success",
        "model": "m",
        "request": {
            "body": {
                "messages": [{"role": "user", "content": "hash:abc123"}],
                "system": "hash:abc123",
            },
        },
        "response": {
            "choices": [{"message": {"content": "hash:abc123"}}],
            "usage": {"prompt_tokens": 10},
        },
        "error": None,
    }
    out = normalize_record_unresolved(rec)
    assert out["messages"] == [{"role": "user", "content": "hash:abc123"}]
    assert out["system"] == "hash:abc123"
    assert out["response"]["content"] == "hash:abc123"


def test_normalize_record_resolves_hashes():
    """normalize_record resolves hash pointers in request and response."""
    strings = {"hash:abc123": "resolved content"}
    rec = {
        "timing": {"start": 1.0, "completionStart": None, "end": 2.0},
        "status": "success",
        "model": "m",
        "request": {
            "body": {
                "messages": [{"role": "user", "content": "hash:abc123"}],
                "system": "hash:abc123",
            },
        },
        "response": {
            "choices": [{"message": {"content": "hash:abc123"}}],
            "usage": {"prompt_tokens": 10},
        },
        "error": None,
    }
    out = normalize_record(rec, strings)
    assert out["messages"] == [{"role": "user", "content": "resolved content"}]
    assert out["system"] == "resolved content"
    assert out["response"]["content"] == "resolved content"


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


def test_normalize_extracts_finish_reason():
    rec = _record_with_choice(
        {
            "message": {"role": "assistant", "content": "cut off —"},
            "finish_reason": "content_filter",
        }
    )
    assert normalize_record_unresolved(rec)["finish_reason"] == "content_filter"


def test_normalize_extracts_finish_reason_without_a_message():
    """The reason is a sibling of `message`, so it survives `message` being absent."""
    out = normalize_record_unresolved(_record_with_choice({"finish_reason": "length"}))
    assert out["finish_reason"] == "length"
    assert out["response"] == {}


def test_normalize_finish_reason_is_none_when_absent():
    rec = _record_with_choice({"message": {"role": "assistant", "content": "hi"}})
    assert normalize_record_unresolved(rec)["finish_reason"] is None
    assert normalize_record_unresolved(_raw_record())["finish_reason"] is None


def test_normalize_finish_reason_is_none_without_choices_or_response():
    no_choices = cast("LogRecord", {"response": {"usage": {}}})
    assert normalize_record_unresolved(no_choices)["finish_reason"] is None
    assert normalize_record_unresolved(cast("LogRecord", {}))["finish_reason"] is None


def test_normalize_finish_reason_ignores_a_non_string_value():
    """A malformed reason is dropped rather than handed to the viewer to render."""
    rec = _record_with_choice({"message": {"content": "hi"}, "finish_reason": {"why": "stop"}})
    assert normalize_record_unresolved(rec)["finish_reason"] is None


def test_normalize_extracts_max_tokens():
    """The viewer needs the cap to tell a real truncation from a max_tokens:1 probe."""
    rec = _raw_record()
    rec["request"]["body"]["data"]["max_tokens"] = 64000
    assert normalize_record_unresolved(rec)["max_tokens"] == 64000


def test_normalize_max_tokens_is_none_when_absent_or_malformed():
    assert normalize_record_unresolved(_raw_record())["max_tokens"] is None
    rec = _raw_record()
    rec["request"]["body"]["data"]["max_tokens"] = "64000"
    assert normalize_record_unresolved(rec)["max_tokens"] is None
    # bool is an int subclass, so True would otherwise be reported as a cap of 1.
    rec["request"]["body"]["data"]["max_tokens"] = True
    assert normalize_record_unresolved(rec)["max_tokens"] is None
