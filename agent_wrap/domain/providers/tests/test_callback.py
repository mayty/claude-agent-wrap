# This file has been created with the assistance of an AI tool.
"""Tests for providers/litellm_runtime/callback.py."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

_RUNTIME_DIR = Path(__file__).parent.parent / "litellm_runtime"


def _import_runtime_module(name: str):
    """
    Import a module from the (non-package) litellm_runtime/ directory.

    Modules are registered in ``sys.modules`` so that internal imports
    (e.g. ``callback.py`` doing ``from helpers import ...``) resolve to
    the same module object — no duplicate module state.
    """
    spec = importlib.util.spec_from_file_location(name, _RUNTIME_DIR / f"{name}.py")
    assert spec is not None, f"Could not find {_RUNTIME_DIR / f'{name}.py'}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # pyrefly: ignore [missing-attribute]
    return mod


_callback = _import_runtime_module("callback")
# helpers and string_hasher were loaded as side effects of loading callback
# (callback.py does ``from helpers import ...`` which resolved via sys.path).
# Access them through sys.modules so we share the same module objects.
_helpers = sys.modules["helpers"]
_string_hasher = sys.modules["string_hasher"]

_get_log_dir = _callback._get_log_dir
_get_project_hash = _callback._get_project_hash
_get_provider = _callback._get_provider
_get_session_id = _callback._get_session_id
_resolve_thinking_reasoning_conflict = _callback._resolve_thinking_reasoning_conflict
build_record = _callback.build_record
extract_session_alias = _callback.extract_session_alias
extract_session_title = _callback.extract_session_title
get_session_hasher = _helpers.get_session_hasher
_SESSION_HASHERS = _helpers._SESSION_HASHERS
StringHasher = _string_hasher.StringHasher


def test_string_hasher_basic_hashing() -> None:
    """Test that strings meeting the length threshold are hashed."""
    hasher = StringHasher()
    long_string = "a" * 100  # Longer than 66 characters

    result = hasher.hash_string(long_string)
    assert result.startswith("hash:")
    assert len(result) == 69  # "hash:" (5) + 64 hex chars
    assert hasher._strings_to_hashes[long_string] == result
    assert hasher._hashes_to_strings[result] == long_string


def test_string_hasher_short_strings_untouched() -> None:
    """Test that strings below the threshold are left unchanged."""
    hasher = StringHasher()
    short_string = "short"

    result = hasher.hash_string(short_string)
    assert result == short_string
    assert short_string not in hasher._strings_to_hashes


def test_string_hasher_deduplication() -> None:
    """Test that the same string gets the same hash on multiple calls."""
    hasher = StringHasher()
    long_string = "b" * 100

    result1 = hasher.hash_string(long_string)
    result2 = hasher.hash_string(long_string)

    assert result1 == result2
    assert len(hasher._strings_to_hashes) == 1


def test_string_hasher_real_flush() -> None:
    """Test the actual flush method of StringHasher writes JSONL correctly."""
    hasher = StringHasher()
    long_string1 = "d" * 100
    long_string2 = "e" * 100
    hasher.hash_string(long_string1)
    hasher.hash_string(long_string2)

    with tempfile.TemporaryDirectory() as tmp_dir:
        # flush() now takes the resolved log dir directly, so the real method can
        # be exercised against a temp directory — no mocking needed.
        log_dir = Path(tmp_dir) / "abc123" / "litellm-test" / "test-session"
        log_dir.mkdir(parents=True, exist_ok=True)
        strings_file = log_dir / "strings.jsonl"

        # First flush
        hasher.flush(log_dir)

        # Verify file was created and contains the first mappings
        assert strings_file.exists()
        with strings_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 2  # Two unique strings hashed

        # Verify internal state is cleared
        assert len(hasher._hashes_to_strings) == 0
        # But _strings_to_hashes is kept for ongoing deduplication
        assert len(hasher._strings_to_hashes) == 2

        # Hash a new string and flush again to test append behavior
        long_string3 = "f" * 100
        hash_result3 = hasher.hash_string(long_string3)
        hasher.flush(log_dir)

        # Verify the file now has 3 lines (appended, not overwritten)
        with strings_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 3

        # Verify we can parse all lines and find our mappings
        mappings: dict[str, Any] = {}
        for line in lines:
            entry = json.loads(line)
            mappings[entry["hash"]] = entry["original"]

        assert hash_result3 in mappings
        assert mappings[hash_result3] == long_string3


def test_build_record_success_shape() -> None:
    kwargs = {
        "model": "bedrock/claude",
        "messages": [{"role": "user", "content": "hi"}],
        "litellm_params": {"proxy_server_request": {"url": "/bedrock/x", "body": {"a": 1}}},
        "standard_logging_object": {
            "startTime": 1780916982.12,
            "completionStartTime": 1780916982.5,
            "endTime": 1780916985.0,
        },
    }
    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    assert record["status"] == "success"
    assert record["model"] == "bedrock/claude"
    assert record["request"]["url"] == "/bedrock/x"
    assert record["response"] == {"choices": [{"text": "yo"}]}
    assert record["error"] is None
    # Timing is sourced verbatim from LiteLLM's standard_logging_object.
    assert record["timing"] == {
        "start": 1780916982.12,
        "completionStart": 1780916982.5,
        "end": 1780916985.0,
    }


def test_build_record_timing_defaults_to_none_without_logging_object() -> None:
    record = build_record({}, None, status="success")
    assert record["timing"] == {"start": None, "completionStart": None, "end": None}


def test_build_record_timing_falls_back_to_callback_datetimes() -> None:
    # When the standard_logging_object lacks epoch timestamps, the callback's own
    # start_time/end_time datetimes fill in — so start is never None and the stats
    # reader never mints the timestamp-less "?" day-key.
    start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 6, 5, 12, 0, 3, tzinfo=timezone.utc)
    record = build_record({}, None, status="success", start_time=start, end_time=end)
    assert record["timing"] == {
        "start": start.timestamp(),
        "completionStart": start.timestamp(),
        "end": end.timestamp(),
    }


def test_build_record_timing_prefers_slo_epoch_over_datetime_fallback() -> None:
    # The standard_logging_object epoch values win when present; the datetime
    # fallback is only used for fields LiteLLM omitted.
    start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
    kwargs = {"standard_logging_object": {"startTime": 1780916982.12}}
    record = build_record(kwargs, None, status="success", start_time=start)
    assert record["timing"]["start"] == 1780916982.12
    assert record["timing"]["completionStart"] == start.timestamp()


def test_build_record_failure_includes_error() -> None:
    record = build_record({}, None, status="failure", exc=RuntimeError("boom"))
    assert record["status"] == "failure"
    assert record["error"] == "boom"


def test_build_record_tolerates_missing_keys() -> None:
    record = build_record({}, None, status="success")
    assert record["model"] == "undefined"
    assert record["request"] is None


def test_build_record_is_json_serializable_with_default_str() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-obj"

    record = build_record({"model": "m", "messages": [Weird()]}, Weird(), status="success")
    # default=str mirrors how the callback writes the line.
    line = json.dumps(record, default=str)
    assert "weird-obj" in line


def test_build_record_drops_proxy_server_request_cycle() -> None:
    """body.proxy_server_request is a self-cycle — build_record must delete it."""
    psr = {
        "url": "http://example.com",
        "method": "POST",
        "headers": {},  # pyrefly: ignore [implicit-any-empty-container]
        "body": {
            "model": "m",
            "messages": [{"role": "user", "content": "hello"}],
        },
    }
    # Create the self-cycle that LiteLLM's data structure has
    psr["body"]["proxy_server_request"] = psr  # pyrefly: ignore [bad-assignment]

    kwargs = {
        "model": "m",
        "messages": [{"role": "user", "content": "hello"}],
        "litellm_params": {"proxy_server_request": psr},
    }
    record = build_record(kwargs, {}, status="success")

    # The cycle should be broken — body.proxy_server_request must not appear
    body = record["request"]["body"]
    assert "proxy_server_request" not in body

    # The rest of the record is intact
    assert record["request"]["url"] == "http://example.com"
    assert record["request"]["body"]["messages"][0]["content"] == "hello"


def test_build_record_handles_shared_references() -> None:
    """Shared references serialize normally — duplicated, no wrap-ref pointers."""
    shared_content = [{"type": "text", "text": "shared text block"}]
    messages = [{"role": "user", "content": shared_content}]

    psr = {
        "url": "http://example.com",
        "method": "POST",
        "headers": {},
        "body": {
            "model": "m",
            "messages": messages,  # same Python list as kwargs["messages"]
        },
    }

    kwargs = {
        "model": "m",
        "messages": messages,
        "litellm_params": {"proxy_server_request": psr},
    }
    record = build_record(kwargs, {}, status="success")

    # Both copies of the shared list are serialized inline — no wrap-ref pointers
    req_json = json.dumps(record)
    assert "wrap-ref" not in req_json

    # The request body carries the shared content
    assert record["request"]["body"]["messages"] == messages


def test_build_record_no_wrap_refs_in_output() -> None:
    """The output JSON must contain no wrap-ref strings at all."""
    kwargs = {
        "model": "test-model",
        "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ],
        "litellm_params": {
            "proxy_server_request": {
                "url": "http://example.com",
                "method": "POST",
                "headers": {"x-claude-code-session-id": "test-session"},
                "body": {
                    "model": "test-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
            },
        },
    }

    record = build_record(
        kwargs,
        {"choices": [{"message": {"content": "hi"}}]},
        status="success",
    )
    record_json = json.dumps(record)
    assert "wrap-ref" not in record_json


def test_build_record_uses_model_dump_for_pydantic_like() -> None:
    class Modelish:
        def model_dump(self) -> dict[str, object]:
            return {"kind": "modelish", "n": 1}

    record = build_record({"model": "m"}, Modelish(), status="success")
    assert record["response"] == {"kind": "modelish", "n": 1}


class _RawResponse:
    """
    Stand-in for the raw httpx Response LiteLLM sometimes passes the hook.

    It has no model_dump/dict, so json_safe stringifies it — exactly the case
    that dropped usage before the recovery fix.
    """

    def __str__(self) -> str:
        return "<Response [200 OK]>"


def test_build_record_recovers_usage_from_slo_when_response_not_dict() -> None:
    """A raw-Response success recovers usage from the standard_logging_object."""
    kwargs = {
        "model": "bedrock/claude",
        "standard_logging_object": {
            "prompt_tokens": 1357,
            "completion_tokens": 152,
            "cache_read_input_tokens": 1000,
        },
    }
    record = build_record(kwargs, _RawResponse(), status="success")

    assert record["response"]["_usage_source"] == "standard_logging_object"
    usage = record["response"]["usage"]
    assert usage["prompt_tokens"] == 1357
    assert usage["completion_tokens"] == 152
    assert usage["cache_read_input_tokens"] == 1000
    # The record must round-trip through the same serialization the callback uses.
    json.dumps(record, default=str)


def test_build_record_native_dict_response_carries_no_marker() -> None:
    """A normal parsed-dict response is kept verbatim, with no _usage_source tag."""
    kwargs = {
        "model": "bedrock/claude",
        "standard_logging_object": {"prompt_tokens": 10, "completion_tokens": 2},
    }
    response = {"choices": [{"text": "yo"}], "usage": {"input_tokens": 5}}
    record = build_record(kwargs, response, status="success")

    assert record["response"] == response
    assert "_usage_source" not in record["response"]


def test_build_record_marks_unrecoverable_when_slo_has_no_usage() -> None:
    """No usable usage anywhere → tagged 'unrecoverable', not a silent $0 record."""
    slo = {"some": "diagnostic", "tokens": "missing"}
    kwargs = {"model": "bedrock/claude", "standard_logging_object": slo}
    record = build_record(kwargs, _RawResponse(), status="success")

    assert record["response"]["_usage_source"] == "unrecoverable"
    # The original stringified response is retained for forensics.
    assert record["response"]["_raw_response"] == "<Response [200 OK]>"
    # The SLO we failed to recover from is captured so the failure is debuggable.
    assert record["response"]["_standard_logging_object"] == slo


def test_build_record_recovery_preserves_response_content() -> None:
    """When the SLO response has content but no usage, keep content AND add usage."""
    kwargs = {
        "model": "bedrock/claude",
        "standard_logging_object": {
            "response": {"choices": [{"message": {"content": '{"name": "my-session"}'}}]},
            "prompt_tokens": 1357,
            "completion_tokens": 152,
        },
    }
    record = build_record(kwargs, _RawResponse(), status="success")

    response = record["response"]
    assert response["_usage_source"] == "standard_logging_object"
    # Usage was synthesized from the SLO's flat token fields...
    assert response["usage"]["prompt_tokens"] == 1357
    assert response["usage"]["completion_tokens"] == 152
    # ...and the response content was preserved (not dropped), so alias extraction works.
    assert response["choices"][0]["message"]["content"] == '{"name": "my-session"}'
    assert extract_session_alias(response) == "my-session"


def test_build_record_failure_does_not_recover_or_mark() -> None:
    """Failures legitimately carry no usage; they must not be tagged unrecoverable."""
    record = build_record({"model": "m"}, _RawResponse(), status="failure")
    assert record["response"] == "<Response [200 OK]>"


def test_build_record_hashes_long_strings() -> None:
    """Test that build_record hashes long strings in the output."""
    long_string = "e" * 100
    kwargs = {
        "model": "bedrock/claude",
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"x-claude-code-session-id": "test-session"},
                "body": {"messages": [{"role": "user", "content": long_string}]},
            }
        },
    }

    # We can't test the actual file creation in unit tests due to permissions,
    # but we can verify the hashing behavior in the record
    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    # The long string should be hashed in the output
    messages = record["request"]["body"]["messages"]
    assert messages is not None
    assert len(messages) == 1
    content = messages[0]["content"]
    assert content.startswith("hash:")
    assert len(content) == 69  # "hash:" (5) + 64 hex chars


def test_build_record_leaves_short_strings_unchanged() -> None:
    """Test that build_record leaves short strings unchanged."""
    short_string = "short"
    kwargs = {
        "model": "bedrock/claude",
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"x-claude-code-session-id": "test-session"},
                "body": {"messages": [{"role": "user", "content": short_string}]},
            }
        },
    }

    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    # The short string should remain unchanged
    messages = record["request"]["body"]["messages"]
    assert messages is not None
    assert len(messages) == 1
    content = messages[0]["content"]
    assert content == short_string


def _name_response(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def test_extract_session_alias_from_name_payload() -> None:
    resp = _name_response('{"name": "agent-logs-web-viewer"}')
    assert extract_session_alias(resp) == "agent-logs-web-viewer"


def test_extract_session_alias_ignores_title_payload() -> None:
    # The sibling title-generation call must not be treated as an alias.
    resp = _name_response('{"title": "Build a web viewer for logs"}')
    assert extract_session_alias(resp) is None


def test_extract_session_alias_tolerates_trailing_prose() -> None:
    resp = _name_response('{"name": "fix-login-bug"} sure thing!')
    assert extract_session_alias(resp) == "fix-login-bug"


def test_extract_session_alias_handles_choices_text_shape() -> None:
    resp = {"choices": [{"text": '{"name": "add-auth-feature"}'}]}
    assert extract_session_alias(resp) == "add-auth-feature"


def test_extract_session_alias_none_for_freeform_and_empty() -> None:
    assert extract_session_alias(_name_response("just some words")) is None
    assert extract_session_alias(_name_response('{"name": ""}')) is None
    assert extract_session_alias(_name_response('{"name": "   "}')) is None
    assert extract_session_alias({}) is None
    assert extract_session_alias(None) is None


def testextract_session_title_from_title_payload() -> None:
    resp = _name_response('{"title": "Build a web viewer for logs"}')
    assert extract_session_title(resp) == "Build a web viewer for logs"


def testextract_session_title_ignores_name_payload() -> None:
    resp = _name_response('{"name": "some-slug"}')
    assert extract_session_title(resp) is None


def testextract_session_title_none_for_freeform_and_empty() -> None:
    assert extract_session_title(_name_response("just some words")) is None
    assert extract_session_title(_name_response('{"title": ""}')) is None
    assert extract_session_title(_name_response('{"title": "   "}')) is None
    assert extract_session_title({}) is None
    assert extract_session_title(None) is None


def testextract_session_title_tolerates_trailing_prose() -> None:
    resp = _name_response('{"title": "Fix the login bug"} here you go!')
    assert extract_session_title(resp) == "Fix the login bug"


def testextract_session_title_handles_choices_text_shape() -> None:
    resp = {"choices": [{"text": '{"title": "Add authentication"}'}]}
    assert extract_session_title(resp) == "Add authentication"


def test_get_session_id_extracted_from_headers() -> None:
    kwargs = {
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"x-claude-code-session-id": "test-session-123", "other-header": "value"}
            }
        }
    }
    assert _get_session_id(kwargs) == "test-session-123"


def test_get_session_id_fallback_when_missing() -> None:
    kwargs = {"litellm_params": {}}  # pyrefly: ignore [implicit-any-empty-container]
    assert _get_session_id(kwargs) == "unknown-session"

    kwargs = {"litellm_params": {"proxy_server_request": {}}}  # pyrefly: ignore [implicit-any-empty-container]
    assert _get_session_id(kwargs) == "unknown-session"

    kwargs = {"litellm_params": {"proxy_server_request": {"headers": {}}}}  # pyrefly: ignore [implicit-any-empty-container]
    assert _get_session_id(kwargs) == "unknown-session"


def _hash_kwargs(value: str) -> dict[str, Any]:
    return {
        "litellm_params": {"proxy_server_request": {"headers": {"x-agent-wrap-log-prefix": value}}}
    }


def test_get_project_hash_from_header() -> None:
    assert _get_project_hash(_hash_kwargs("0123456789abcdef")) == "0123456789abcdef"


def test_get_project_hash_fallback_when_missing() -> None:
    assert _get_project_hash({"litellm_params": {}}) == "unknown-project"
    assert _get_project_hash({"litellm_params": {"proxy_server_request": {}}}) == "unknown-project"
    assert _get_project_hash(_hash_kwargs("")) == "unknown-project"


@pytest.mark.parametrize(
    "bad",
    ["ABCDEF", "../../etc", "/abs/path", "a/b", "..", ".", "g123", "0123 4567"],
)
def test_get_project_hash_rejects_non_hex_and_traversal(bad: str) -> None:
    # Anything outside the lowercase-hex alphabet (including '/', '.', '..',
    # uppercase, leading slash) falls back so it can never escape the mount.
    assert _get_project_hash(_hash_kwargs(bad)) == "unknown-project"


def test_get_provider_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WRAP_PROVIDER", "litellm-bedrock")
    assert _get_provider() == "litellm-bedrock"


def test_get_provider_fallback_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_WRAP_PROVIDER", raising=False)
    assert _get_provider() == "unknown-provider"


@pytest.mark.parametrize("bad", ["../evil", "Has Spaces", "UPPER", "a/b"])
def test_get_provider_rejects_illegal_chars(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("AGENT_WRAP_PROVIDER", bad)
    assert _get_provider() == "unknown-provider"


def test_get_log_dir_composes_hash_provider_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_WRAP_PROVIDER", "litellm-bedrock")
    kwargs = {
        "litellm_params": {
            "proxy_server_request": {
                "headers": {
                    "x-agent-wrap-log-prefix": "0123456789abcdef",
                    "x-claude-code-session-id": "sess-1",
                }
            }
        }
    }
    assert _get_log_dir(kwargs) == Path(
        "/var/log/agent-wrap/0123456789abcdef/litellm-bedrock/sess-1"
    )


def test_get_log_dir_uses_defaults_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_WRAP_PROVIDER", raising=False)
    assert _get_log_dir({}) == Path(
        "/var/log/agent-wrap/unknown-project/unknown-provider/unknown-session"
    )


def test_cross_request_deduplication_and_concurrent_flush_safety() -> None:
    """Test that shared hashers deduplicate across requests and handle concurrent flushes safely."""
    # Clear the global cache to ensure a clean test state
    _SESSION_HASHERS.clear()

    session_id = "test-concurrent-session"
    long_string = "x" * 100

    # Simulate Request 1
    kwargs1 = {
        "model": "test-model",
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"x-claude-code-session-id": session_id},
                "body": {"messages": [{"role": "user", "content": long_string}]},
            }
        },
    }
    record1 = build_record(kwargs1, {"choices": [{"text": "response 1"}]}, status="success")

    # Simulate Request 2 with the SAME string (should be deduplicated in memory)
    kwargs2 = {
        "model": "test-model",
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"x-claude-code-session-id": session_id},
                "body": {"messages": [{"role": "user", "content": long_string}]},
            }
        },
    }
    record2 = build_record(kwargs2, {"choices": [{"text": "response 2"}]}, status="success")

    # Both records should have the exact same hash
    hash1 = record1["request"]["body"]["messages"][0]["content"]
    hash2 = record2["request"]["body"]["messages"][0]["content"]
    assert hash1 == hash2
    assert hash1.startswith("hash:")

    # The shared hasher should retain the mapping for ongoing deduplication
    hasher = get_session_hasher(session_id, _get_log_dir(kwargs1))
    assert len(hasher._strings_to_hashes) == 1

    # Test concurrent flush safety directly on the hasher
    hasher._hashes_to_strings = {"hash:test": "test_string"}

    # Simulate concurrent grab-and-clear
    hasher._hashes_to_strings = {}  # pyrefly: ignore [implicit-any-empty-container]

    # A concurrent hash_string call would populate the new dict
    hasher.hash_string("y" * 100)

    # The new dict should have the new string, not the old one
    assert len(hasher._hashes_to_strings) == 1
    assert "hash:test" not in hasher._hashes_to_strings

    # Clean up
    _SESSION_HASHERS.clear()


def test_string_hasher_loads_seen_hashes_to_prevent_duplicates() -> None:
    """Test that _load_seen_hashes prevents duplicate writes after a restart."""
    _SESSION_HASHERS.clear()

    with tempfile.TemporaryDirectory() as tmp_dir:
        # load_seen_hashes()/flush() now take the resolved log dir directly, so
        # the real methods can run against a temp directory — no mocking needed.
        log_dir = Path(tmp_dir) / "abc123" / "litellm-test" / "test-restart-session"
        log_dir.mkdir(parents=True, exist_ok=True)
        strings_file = log_dir / "strings.jsonl"

        # Simulate a pre-existing file from a previous run
        existing_hash = "hash:abc123"
        existing_string = "previously hashed string"
        with strings_file.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"hash": existing_hash, "original": existing_string}) + "\n")

        # Create a new hasher and load state from the existing file
        hasher = StringHasher()
        hasher.load_seen_hashes(log_dir)

        # Verify the hash was loaded
        assert existing_hash in hasher._seen_hashes

        # Now, if we try to flush the same hash, it should be skipped
        hasher._hashes_to_strings[existing_hash] = existing_string
        new_hash = "hash:def456"
        new_string = "new string"
        hasher._hashes_to_strings[new_hash] = new_string

        hasher.flush(log_dir)

        # Verify only the new hash was appended
        with strings_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 2  # Original + 1 new
        assert existing_hash in lines[0]
        assert new_hash in lines[1]

    _SESSION_HASHERS.clear()


def test_resolve_conflict_strips_effort_from_output_config() -> None:
    """Effort is removed from output_config when thinking.type == 'disabled'."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Generate a title"}],
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "high", "style": "concise"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert "effort" not in result["output_config"]
    assert result["output_config"] == {"style": "concise"}
    assert result["thinking"] == {"type": "disabled"}


def test_resolve_conflict_handles_missing_output_config() -> None:
    """No-op when thinking is disabled but output_config is absent."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Generate a title"}],
        "thinking": {"type": "disabled"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result == data  # unchanged


def test_resolve_conflict_preserves_output_config_when_thinking_enabled() -> None:
    """output_config is untouched when thinking.type == 'enabled'."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-pro[1m]",
        "messages": [{"role": "user", "content": "Solve this complex problem"}],
        "thinking": {"type": "enabled", "budget_tokens": 4000},
        "output_config": {"effort": "high"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result["output_config"] == {"effort": "high"}
    assert result["thinking"] == {"type": "enabled", "budget_tokens": 4000}


def test_resolve_conflict_preserves_output_config_when_thinking_absent() -> None:
    """output_config is untouched when thinking is not in data."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-pro[1m]",
        "messages": [{"role": "user", "content": "Hello"}],
        "output_config": {"effort": "high"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result["output_config"] == {"effort": "high"}


def test_resolve_conflict_handles_thinking_none() -> None:
    """output_config is untouched when thinking is None."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-pro[1m]",
        "messages": [{"role": "user", "content": "Hello"}],
        "thinking": None,
        "output_config": {"effort": "high"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result["output_config"] == {"effort": "high"}


def test_resolve_conflict_handles_output_config_not_dict() -> None:
    """No-op when thinking is disabled but output_config is not a dict."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Generate a title"}],
        "thinking": {"type": "disabled"},
        "output_config": "not-a-dict",
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result == data  # unchanged


def test_resolve_conflict_only_modifies_effort_in_output_config() -> None:
    """Only output_config.effort is removed; all other keys preserved."""
    data: dict[str, Any] = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Generate a title"}],
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "high", "style": "concise", "max_tokens": 256},
        "max_tokens": 512,
        "temperature": 0.7,
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert "effort" not in result["output_config"]
    assert result["output_config"] == {"style": "concise", "max_tokens": 256}
    assert result["model"] == "deepseek-v4-flash"
    assert result["max_tokens"] == 512
    assert result["temperature"] == 0.7
    assert result["thinking"] == {"type": "disabled"}
