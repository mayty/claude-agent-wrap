from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, cast

from agent_wrap.domain.logs.normalize import extract_alias, normalize_record

if TYPE_CHECKING:
    from agent_wrap.domain.providers.litellm_common.models import LogRecord


def _epoch(iso: str) -> float:
    """ISO-8601 string -> Unix epoch seconds."""
    return datetime.fromisoformat(iso).timestamp()


# --- normalize_record ---


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


# --- normalize_record hash resolution ---


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


# --- extract_alias ---


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
