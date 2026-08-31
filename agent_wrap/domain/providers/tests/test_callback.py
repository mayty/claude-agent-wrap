# This file has been created with the assistance of an AI tool.
"""Tests for providers/litellm_runtime/callback.py."""

import asyncio
import importlib.util
import json
import sys
import tempfile
import types
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, override

import pytest

_RUNTIME_DIR = Path(__file__).parent.parent / "litellm_runtime"


def _import_runtime_module(name: str) -> Any:
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

_claim_failure = _callback._claim_failure
_get_log_dir = _callback._get_log_dir
_get_project_hash = _callback._get_project_hash
_get_provider = _callback._get_provider
_get_request_headers = _callback._get_request_headers
_get_session_id = _callback._get_session_id
_record_failure = _callback._record_failure
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
    start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    end = datetime(2026, 6, 5, 12, 0, 3, tzinfo=UTC)
    record = build_record({}, None, status="success", start_time=start, end_time=end)
    assert record["timing"] == {
        "start": start.timestamp(),
        "completionStart": start.timestamp(),
        "end": end.timestamp(),
    }


def test_build_record_timing_prefers_slo_epoch_over_datetime_fallback() -> None:
    # The standard_logging_object epoch values win when present; the datetime
    # fallback is only used for fields LiteLLM omitted.
    start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
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


def test_build_record_keeps_passthrough_headers_readable() -> None:
    """
    A Mapping from the /anthropic/* passthrough route survives as a real object.

    The logs viewer reads x-claude-code-agent-id out of request.headers to split
    subagent threads from the main one, so a header set flattened to a string
    collapses every subagent into the main tab. Credentials are redacted on the
    way through.
    """

    class FakeHeaders(Mapping[str, str]):
        def __init__(self, data: dict[str, str]) -> None:
            self._data = data

        @override
        def __getitem__(self, key: str) -> str:
            return self._data[key]

        @override
        def __iter__(self) -> Iterator[str]:
            return iter(self._data)

        @override
        def __len__(self) -> int:
            return len(self._data)

    headers = FakeHeaders(
        {
            "authorization": "Bearer sk-ant-oat01-" + "s" * 100,
            "x-claude-code-session-id": "test-session",
            "x-claude-code-agent-id": "a52736f97cfe0ad52",
        }
    )
    kwargs = {
        "model": "m",
        "litellm_params": {
            "proxy_server_request": {
                "url": "http://example.com",
                "method": "POST",
                "headers": headers,
                "body": {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            }
        },
    }

    record = build_record(kwargs, {}, status="success")

    recorded = record["request"]["headers"]
    assert recorded["x-claude-code-agent-id"] == "a52736f97cfe0ad52"
    assert recorded["authorization"] == "<redacted>"
    # The live structure LiteLLM still holds must keep its real credential.
    assert headers["authorization"].startswith("Bearer sk-ant-oat01-")


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


@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param(
            {"proxy_server_request": {"headers": {"x-agent-wrap-log-prefix": "0123456789abcdef"}}},
            id="top-level-proxy-server-request",
        ),
        pytest.param(
            {
                "litellm_params": {
                    "metadata": {"headers": {"x-agent-wrap-log-prefix": "0123456789abcdef"}}
                }
            },
            id="litellm-params-metadata",
        ),
    ],
)
def test_get_project_hash_reads_fallback_header_locations(kwargs: dict[str, Any]) -> None:
    """
    Resolve the real hash from every known header location, not just the primary.

    A record filed under unknown-project is written to disk but invisible in the
    viewer — the same silent-drop failure this callback exists to prevent.
    """
    assert _get_project_hash(kwargs) == "0123456789abcdef"


def test_get_session_id_reads_fallback_header_location() -> None:
    kwargs = {"litellm_params": {"metadata": {"headers": {"x-claude-code-session-id": "sess-9"}}}}
    assert _get_session_id(kwargs) == "sess-9"


def test_get_request_headers_prefers_primary_over_fallback() -> None:
    kwargs = {
        "litellm_params": {
            "proxy_server_request": {"headers": {"x-claude-code-session-id": "primary"}},
            "metadata": {"headers": {"x-claude-code-session-id": "fallback"}},
        }
    }
    assert _get_session_id(kwargs) == "primary"


def test_get_request_headers_empty_when_no_location_matches() -> None:
    assert (
        _get_request_headers({"litellm_params": {"proxy_server_request": {"headers": None}}}) == {}
    )


@pytest.mark.parametrize("bad", ["ABCDEF", "../../etc", "/abs/path", "a/b", ".."])
def test_get_project_hash_validates_fallback_locations_too(bad: str) -> None:
    """The traversal guard must cover every candidate location, not only the first."""
    kwargs = {"litellm_params": {"metadata": {"headers": {"x-agent-wrap-log-prefix": bad}}}}
    assert _get_project_hash(kwargs) == "unknown-project"


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


def _load_callback_with_stub_litellm(tmp_path: Path) -> Any:
    """
    Re-import callback.py with a stub ``litellm`` so ``FileLogger`` is defined.

    The real ``litellm`` is only installed inside the sidecar container, so in the
    dev env ``callback.py`` takes its ImportError branch and ``FileLogger`` never
    exists. Supplying a minimal ``CustomLogger`` base lets the hooks be exercised
    for real rather than asserted about indirectly. The log root is redirected to
    *tmp_path* so nothing touches /var/log.
    """
    litellm_mod = types.ModuleType("litellm")
    integrations = types.ModuleType("litellm.integrations")
    custom_logger = types.ModuleType("litellm.integrations.custom_logger")

    class _CustomLogger:
        pass

    class _RecordingCallbackManager:
        """Records what callback.py registers, so the wiring can be asserted."""

        def __init__(self) -> None:
            self.async_success: list[Any] = []
            self.async_failure: list[Any] = []

        def add_litellm_async_success_callback(self, callback: Any) -> None:
            self.async_success.append(callback)

        def add_litellm_async_failure_callback(self, callback: Any) -> None:
            self.async_failure.append(callback)

    custom_logger.CustomLogger = _CustomLogger  # pyrefly: ignore [missing-attribute]
    litellm_mod.logging_callback_manager = _RecordingCallbackManager()  # pyrefly: ignore [missing-attribute]
    saved = {
        name: sys.modules.get(name)
        for name in ("litellm", "litellm.integrations", "litellm.integrations.custom_logger")
    }
    sys.modules["litellm"] = litellm_mod
    sys.modules["litellm.integrations"] = integrations
    sys.modules["litellm.integrations.custom_logger"] = custom_logger
    mod: Any
    try:
        mod = _import_runtime_module("callback")
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        # Restore the module-under-test that the rest of this file shares.
        sys.modules["callback"] = _callback

    def _log_dir(kwargs: dict[str, Any]) -> Path:
        return tmp_path / _callback._get_session_id(kwargs)

    mod._get_log_dir = _log_dir
    # Expose the stub manager so tests can assert what got registered.
    mod._test_callback_manager = litellm_mod.logging_callback_manager
    return mod


def test_post_call_failure_hook_writes_a_failure_record(tmp_path: Path) -> None:
    """
    A non-2xx on LiteLLM's /anthropic/* passthrough route arrives here, not at
    async_log_failure_event. Without this hook those errors — including the 429s
    Anthropic returns for unrecognized traffic — would be missing from
    messages.jsonl entirely.
    """
    mod = _load_callback_with_stub_litellm(tmp_path)
    request_data = {
        "model": "claude-sonnet-5",
        "litellm_params": {
            "proxy_server_request": {
                "url": "/anthropic/v1/messages",
                "method": "POST",
                "headers": {"x-claude-code-session-id": "sess-fail"},
                "body": {"model": "claude-sonnet-5"},
            }
        },
    }

    asyncio.run(
        mod.file_logger_instance.async_post_call_failure_hook(
            request_data=request_data,
            original_exception=RuntimeError("429 Too Many Requests"),
            user_api_key_dict=None,
        )
    )

    log_file = tmp_path / "sess-fail" / "messages.jsonl"
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["status"] == "failure"
    assert record["error"] == "429 Too Many Requests"
    assert record["model"] == "claude-sonnet-5"
    assert record["request"]["url"] == "/anthropic/v1/messages"


def test_post_call_failure_hook_counts_the_record_in_metadata(tmp_path: Path) -> None:
    """meta.json must count passthrough failures like any other logged call."""
    mod = _load_callback_with_stub_litellm(tmp_path)
    request_data = {
        "model": "claude-sonnet-5",
        "litellm_params": {
            "proxy_server_request": {
                "headers": {"x-claude-code-session-id": "sess-meta"},
            }
        },
    }

    asyncio.run(
        mod.file_logger_instance.async_post_call_failure_hook(
            request_data=request_data,
            original_exception=RuntimeError("boom"),
            user_api_key_dict=None,
        )
    )

    meta = json.loads((tmp_path / "sess-meta" / "meta.json").read_text(encoding="utf-8"))
    assert meta["count"] == 1
    assert meta["models"] == ["claude-sonnet-5"]


def test_log_stream_event_writes_a_success_record(tmp_path: Path) -> None:
    """
    LiteLLM routes streaming calls here instead of async_log_success_event when
    model_call_details lacks async_complete_streaming_response. The base-class hook
    is a bare ``pass``, so an unimplemented override would drop those calls from the
    log silently.
    """
    mod = _load_callback_with_stub_litellm(tmp_path)
    kwargs = {
        "model": "claude-sonnet-5",
        "litellm_params": {
            "proxy_server_request": {
                "url": "/anthropic/v1/messages",
                "headers": {"x-claude-code-session-id": "sess-stream"},
            }
        },
    }

    asyncio.run(
        mod.file_logger_instance.async_log_stream_event(
            kwargs=kwargs,
            response_obj={"usage": {"input_tokens": 11, "output_tokens": 22}},
            start_time=datetime(2026, 8, 7, 13, 0, 0, tzinfo=UTC),
            end_time=datetime(2026, 8, 7, 13, 0, 1, tzinfo=UTC),
        )
    )

    log_file = tmp_path / "sess-stream" / "messages.jsonl"
    record = json.loads(log_file.read_text(encoding="utf-8").strip())
    assert record["status"] == "success"
    assert record["model"] == "claude-sonnet-5"
    # Usage must survive: agent stats reads it for token accounting.
    assert record["response"]["usage"]["input_tokens"] == 11


def test_import_registers_logger_for_async_success(tmp_path: Path) -> None:
    """
    Successes are dispatched from litellm._async_success_callback, and callback.py must
    register itself there at import time.

    No config key can do this. `litellm_settings: callbacks:` only fills
    litellm.callbacks (failures only), and `success_callback:` routes through
    _is_async_callable — which is False for a CustomLogger instance, because it has no
    __call__ — so it lands in the sync list that passthrough requests skip. Losing this
    registration silently drops every successful request from the log while failures keep
    appearing, which reads as "working" rather than as broken logging.
    """
    mod = _load_callback_with_stub_litellm(tmp_path)
    registered = mod._test_callback_manager.async_success
    assert registered == [mod.file_logger_instance]


@pytest.fixture
def failure_dedup_reset() -> Iterator[None]:
    """Isolate the module-level failure dedup bookkeeping between tests."""
    _callback._SEEN_FAILURE_IDS.clear()
    _callback._SEEN_FAILURE_ORDER.clear()
    yield
    _callback._SEEN_FAILURE_IDS.clear()
    _callback._SEEN_FAILURE_ORDER.clear()


@pytest.mark.usefixtures("failure_dedup_reset")
def test_claim_failure_blocks_a_repeat_of_the_same_call_id() -> None:
    assert _claim_failure("call-1") is True
    assert _claim_failure("call-1") is False


@pytest.mark.usefixtures("failure_dedup_reset")
def test_claim_failure_allows_distinct_call_ids() -> None:
    assert _claim_failure("call-1") is True
    assert _claim_failure("call-2") is True


@pytest.mark.usefixtures("failure_dedup_reset")
@pytest.mark.parametrize("unusable", [None, "", 123, {}])
def test_claim_failure_records_when_call_id_unusable(unusable: Any) -> None:
    """A duplicate row beats a dropped request — dropping is the bug being fixed."""
    assert _claim_failure(unusable) is True
    assert _claim_failure(unusable) is True


@pytest.mark.usefixtures("failure_dedup_reset")
def test_claim_failure_evicts_oldest_beyond_the_limit() -> None:
    """The set and the deque must not drift apart over a long-lived sidecar."""
    limit = _callback._SEEN_FAILURE_LIMIT
    for i in range(limit):
        assert _claim_failure(f"call-{i}") is True
    assert _claim_failure("call-0") is False

    # One more id evicts the oldest, which becomes claimable again.
    assert _claim_failure("overflow") is True
    assert _claim_failure("call-0") is True
    assert len(_callback._SEEN_FAILURE_IDS) == limit
    assert len(_callback._SEEN_FAILURE_ORDER) == limit


def _failure_kwargs(
    session: str, call_id: str | None, model: str = "claude-opus-5"
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "litellm_params": {
            "proxy_server_request": {
                "url": "/anthropic/v1/messages",
                "method": "POST",
                "headers": {"x-claude-code-session-id": session},
                "body": {"model": model, "stream": True},
            }
        },
    }
    if call_id is not None:
        kwargs["litellm_call_id"] = call_id
    return kwargs


def _logged_records(tmp_path: Path, session: str) -> list[dict[str, Any]]:
    log_file = tmp_path / session / "messages.jsonl"
    if not log_file.exists():
        return []
    return [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line]


def test_failure_callback_is_registered_on_the_async_failure_list(tmp_path: Path) -> None:
    """
    Streaming upstream errors (Anthropic's 529 overloaded_error) are dispatched
    through litellm._async_failure_callback, which the /anthropic/* passthrough
    route leaves empty because function_setup() never runs. Losing this
    registration silently drops every streaming failure while non-streaming ones
    keep appearing — which reads as "logging works" rather than as broken.
    """
    mod = _load_callback_with_stub_litellm(tmp_path)
    assert mod._test_callback_manager.async_failure == [mod.file_logger_instance]


def test_log_failure_event_writes_a_failure_record(tmp_path: Path) -> None:
    mod = _load_callback_with_stub_litellm(tmp_path)
    kwargs = _failure_kwargs("sess-stream", "call-stream")
    kwargs["exception"] = RuntimeError("529 Overloaded")

    asyncio.run(mod.file_logger_instance.async_log_failure_event(kwargs, None, None, None))

    records = _logged_records(tmp_path, "sess-stream")
    assert len(records) == 1
    assert records[0]["status"] == "failure"
    assert records[0]["error"] == "529 Overloaded"
    assert records[0]["model"] == "claude-opus-5"


def test_both_failure_paths_record_the_same_call_id_once(tmp_path: Path) -> None:
    """Both lists are registered, so one upstream error can arrive twice."""
    mod = _load_callback_with_stub_litellm(tmp_path)
    kwargs = _failure_kwargs("sess-dedup", "call-shared")
    kwargs["exception"] = RuntimeError("529 Overloaded")

    asyncio.run(
        mod.file_logger_instance.async_post_call_failure_hook(
            request_data=kwargs,
            original_exception=RuntimeError("529 Overloaded"),
            user_api_key_dict=None,
        )
    )
    asyncio.run(mod.file_logger_instance.async_log_failure_event(kwargs, None, None, None))

    assert len(_logged_records(tmp_path, "sess-dedup")) == 1


def test_both_failure_paths_record_distinct_call_ids_separately(tmp_path: Path) -> None:
    mod = _load_callback_with_stub_litellm(tmp_path)
    for call_id in ("call-a", "call-b"):
        kwargs = _failure_kwargs("sess-distinct", call_id)
        kwargs["exception"] = RuntimeError("529 Overloaded")
        asyncio.run(mod.file_logger_instance.async_log_failure_event(kwargs, None, None, None))

    assert len(_logged_records(tmp_path, "sess-distinct")) == 2


def test_failure_without_call_id_is_still_recorded(tmp_path: Path) -> None:
    mod = _load_callback_with_stub_litellm(tmp_path)
    for _ in range(2):
        kwargs = _failure_kwargs("sess-no-id", None)
        kwargs["exception"] = RuntimeError("529 Overloaded")
        asyncio.run(mod.file_logger_instance.async_log_failure_event(kwargs, None, None, None))

    assert len(_logged_records(tmp_path, "sess-no-id")) == 2


def test_build_record_failure_leaves_completion_start_none() -> None:
    """
    A failed call produced no first token, so completionStart must not inherit the
    call's start — the viewer would otherwise report a 0s time-to-first-token.
    """
    start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    end = datetime(2026, 6, 5, 12, 0, 3, tzinfo=UTC)
    record = build_record({}, None, status="failure", start_time=start, end_time=end)
    assert record["timing"] == {
        "start": start.timestamp(),
        "completionStart": None,
        "end": end.timestamp(),
    }


def test_build_record_failure_keeps_a_real_completion_start() -> None:
    """A call that streamed tokens and then errored still reports its first token."""
    start = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
    kwargs = {"standard_logging_object": {"completionStartTime": 1780916982.5}}
    record = build_record(kwargs, None, status="failure", start_time=start)
    assert record["timing"]["completionStart"] == 1780916982.5


def test_post_call_failure_hook_timestamps_the_record(tmp_path: Path) -> None:
    """
    The passthrough hook is handed no datetime bounds and the passthrough route's
    standard_logging_object has no timestamps, so _record_failure supplies "now".
    Without it the record sorts out of chronological order in the logs viewer and
    meta.json never gets a last_ts.
    """
    mod = _load_callback_with_stub_litellm(tmp_path)
    before = datetime.now(tz=UTC).timestamp()

    asyncio.run(
        mod.file_logger_instance.async_post_call_failure_hook(
            request_data=_failure_kwargs("sess-ts", "call-ts"),
            original_exception=RuntimeError("429 rate_limit_error"),
            user_api_key_dict=None,
        )
    )

    after = datetime.now(tz=UTC).timestamp()
    timing = _logged_records(tmp_path, "sess-ts")[0]["timing"]
    assert before <= timing["start"] <= after
    assert before <= timing["end"] <= after
    assert timing["completionStart"] is None


def test_post_call_failure_hook_records_last_ts_in_metadata(tmp_path: Path) -> None:
    """_write_metadata only sets last_ts from a non-null timing.end."""
    mod = _load_callback_with_stub_litellm(tmp_path)

    asyncio.run(
        mod.file_logger_instance.async_post_call_failure_hook(
            request_data=_failure_kwargs("sess-lastts", "call-lastts"),
            original_exception=RuntimeError("429 rate_limit_error"),
            user_api_key_dict=None,
        )
    )

    meta = json.loads((tmp_path / "sess-lastts" / "meta.json").read_text(encoding="utf-8"))
    assert meta["last_ts"] is not None


def test_log_failure_event_prefers_real_timestamps_over_now(tmp_path: Path) -> None:
    """The synthesized fallback must not displace timing LiteLLM actually reported."""
    mod = _load_callback_with_stub_litellm(tmp_path)
    kwargs = _failure_kwargs("sess-real-ts", "call-real-ts")
    kwargs["exception"] = RuntimeError("529 Overloaded")
    kwargs["standard_logging_object"] = {"startTime": 1780916982.12, "endTime": 1780916985.0}

    asyncio.run(mod.file_logger_instance.async_log_failure_event(kwargs, None, None, None))

    timing = _logged_records(tmp_path, "sess-real-ts")[0]["timing"]
    assert timing["start"] == 1780916982.12
    assert timing["end"] == 1780916985.0
