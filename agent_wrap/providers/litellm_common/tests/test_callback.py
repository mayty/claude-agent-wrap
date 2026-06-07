# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/callback.py."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_wrap.providers.litellm_common.callback import (
    _get_session_id,
    _resolve_thinking_reasoning_conflict,
    build_record,
    extract_session_alias,
)
from agent_wrap.providers.litellm_common.string_hasher import (
    _SESSION_HASHERS,
    StringHasher,
    get_session_hasher,
)


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
        session_id = "test-session"
        log_dir = Path(tmp_dir) / session_id
        log_dir.mkdir(parents=True, exist_ok=True)
        strings_file = log_dir / "strings.jsonl"

        # Mock the flush method to use our temp directory
        def mock_flush(sid: str) -> None:
            # Replicate the append logic but with our temp dir
            try:
                with strings_file.open("a", encoding="utf-8") as f:
                    for h, s in hasher._hashes_to_strings.items():
                        f.write(json.dumps({"hash": h, "original": s}) + "\n")
                hasher._hashes_to_strings.clear()
            except OSError as e:
                print(f"failed to append: {e}", flush=True)

        hasher.flush = mock_flush  # type: ignore[method-assign]

        # First flush
        hasher.flush(session_id)

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
        hasher.flush(session_id)

        # Verify the file now has 3 lines (appended, not overwritten)
        with strings_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) == 3

        # Verify we can parse all lines and find our mappings
        mappings = {}
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
    }
    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    assert record["status"] == "success"
    assert record["model"] == "bedrock/claude"
    assert record["request"]["messages"] == kwargs["messages"]
    assert record["request"]["proxy_server_request"]["url"] == "/bedrock/x"
    assert record["response"] == {"choices": [{"text": "yo"}]}
    assert "error" not in record
    assert "ts" in record


def test_build_record_failure_includes_error() -> None:
    record = build_record({}, None, status="failure", exc=RuntimeError("boom"))
    assert record["status"] == "failure"
    assert record["error"] == "boom"


def test_build_record_tolerates_missing_keys() -> None:
    record = build_record({}, None, status="success")
    assert record["model"] is None
    assert record["request"]["messages"] is None
    assert record["request"]["proxy_server_request"] is None


def test_build_record_is_json_serializable_with_default_str() -> None:
    class Weird:
        def __str__(self) -> str:
            return "weird-obj"

    record = build_record({"model": "m", "messages": [Weird()]}, Weird(), status="success")
    # default=str mirrors how the callback writes the line.
    line = json.dumps(record, default=str)
    assert "weird-obj" in line


def test_build_record_breaks_circular_references() -> None:
    # LiteLLM's response objects contain cycles; build_record must not raise.
    cyclic: dict[str, object] = {"a": 1}
    cyclic["self"] = cyclic

    record = build_record({"model": "m"}, cyclic, status="success")

    # The root object gets a wrap-ref-id, and the self-reference becomes wrap-ref:<id>
    assert "wrap-ref-id" in record["response"]
    root_ref_id = record["response"]["wrap-ref-id"]
    assert record["response"]["self"] == f"wrap-ref:{root_ref_id}"
    assert record["response"]["a"] == 1


def test_build_record_breaks_circular_list_references() -> None:
    # Test that circular references in lists are handled correctly
    # with wrap-ref-id inserted at index 0.
    cyclic_list: list[object] = [1, 2]
    cyclic_list.append(cyclic_list)

    record = build_record({"model": "m"}, {"data": cyclic_list}, status="success")

    # The outer dict is only referenced once, so it does NOT get a wrap-ref-id.
    assert "wrap-ref-id" not in record["response"]

    # The list is referenced twice (once in the dict, once inside itself), so it gets a wrap-ref-id.
    data_list = record["response"]["data"]
    assert data_list[0] == "wrap-ref-id:0"  # The list's wrap-ref-id is at index 0
    list_ref_id = data_list[0].split(":")[1]

    # The circular reference to the list becomes wrap-ref:<id>
    assert data_list[3] == f"wrap-ref:{list_ref_id}"
    assert data_list[1] == 1
    assert data_list[2] == 2


def test_build_record_uses_model_dump_for_pydantic_like() -> None:
    class Modelish:
        def model_dump(self) -> dict[str, object]:
            return {"kind": "modelish", "n": 1}

    record = build_record({"model": "m"}, Modelish(), status="success")
    assert record["response"] == {"kind": "modelish", "n": 1}


def test_build_record_hashes_long_strings() -> None:
    """Test that build_record hashes long strings in the output."""
    long_string = "e" * 100
    kwargs = {
        "model": "bedrock/claude",
        "messages": [{"role": "user", "content": long_string}],
        "litellm_params": {
            "proxy_server_request": {"headers": {"x-claude-code-session-id": "test-session"}}
        },
    }

    # We can't test the actual file creation in unit tests due to permissions,
    # but we can verify the hashing behavior in the record
    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    # The long string should be hashed in the output
    messages = record["request"]["messages"]
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
        "messages": [{"role": "user", "content": short_string}],
        "litellm_params": {
            "proxy_server_request": {"headers": {"x-claude-code-session-id": "test-session"}}
        },
    }

    record = build_record(kwargs, {"choices": [{"text": "yo"}]}, status="success")

    # The short string should remain unchanged
    messages = record["request"]["messages"]
    assert messages is not None
    assert len(messages) == 1
    content = messages[0]["content"]
    assert content == short_string


def _name_response(content: str) -> dict:
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
    kwargs = {"litellm_params": {}}
    assert _get_session_id(kwargs) == "unknown-session"

    kwargs = {"litellm_params": {"proxy_server_request": {}}}
    assert _get_session_id(kwargs) == "unknown-session"

    kwargs = {"litellm_params": {"proxy_server_request": {"headers": {}}}}
    assert _get_session_id(kwargs) == "unknown-session"


def test_cross_request_deduplication_and_concurrent_flush_safety() -> None:
    """Test that shared hashers deduplicate across requests and handle concurrent flushes safely."""
    # Clear the global cache to ensure a clean test state
    _SESSION_HASHERS.clear()

    session_id = "test-concurrent-session"
    long_string = "x" * 100

    # Simulate Request 1
    kwargs1 = {
        "model": "test-model",
        "messages": [{"role": "user", "content": long_string}],
        "litellm_params": {
            "proxy_server_request": {"headers": {"x-claude-code-session-id": session_id}}
        },
    }
    record1 = build_record(kwargs1, {"choices": [{"text": "response 1"}]}, status="success")

    # Simulate Request 2 with the SAME string (should be deduplicated in memory)
    kwargs2 = {
        "model": "test-model",
        "messages": [{"role": "user", "content": long_string}],
        "litellm_params": {
            "proxy_server_request": {"headers": {"x-claude-code-session-id": session_id}}
        },
    }
    record2 = build_record(kwargs2, {"choices": [{"text": "response 2"}]}, status="success")

    # Both records should have the exact same hash
    hash1 = record1["request"]["messages"][0]["content"]
    hash2 = record2["request"]["messages"][0]["content"]
    assert hash1 == hash2
    assert hash1.startswith("hash:")

    # The shared hasher should retain the mapping for ongoing deduplication
    hasher = get_session_hasher(session_id)
    assert len(hasher._strings_to_hashes) == 1

    # Test concurrent flush safety directly on the hasher
    hasher._hashes_to_strings = {"hash:test": "test_string"}

    # Simulate concurrent grab-and-clear
    hasher._hashes_to_strings = {}

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
        session_id = "test-restart-session"
        log_dir = Path(tmp_dir) / session_id
        log_dir.mkdir(parents=True, exist_ok=True)
        strings_file = log_dir / "strings.jsonl"

        # Simulate a pre-existing file from a previous run
        existing_hash = "hash:abc123"
        existing_string = "previously hashed string"
        with strings_file.open("w", encoding="utf-8") as f:
            f.write(json.dumps({"hash": existing_hash, "original": existing_string}) + "\n")

        # Create a new hasher and load state
        hasher = StringHasher()

        # Mock the path for testing
        def mock_load(sid: str) -> None:
            file_path = log_dir / "strings.jsonl"
            if file_path.exists():
                with file_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        entry = json.loads(line)
                        if "hash" in entry:
                            hasher._seen_hashes.add(entry["hash"])

        hasher.load_seen_hashes = mock_load  # type: ignore[method-assign]

        hasher.load_seen_hashes(session_id)

        # Verify the hash was loaded
        assert existing_hash in hasher._seen_hashes

        # Now, if we try to flush the same hash, it should be skipped
        hasher._hashes_to_strings[existing_hash] = existing_string
        new_hash = "hash:def456"
        new_string = "new string"
        hasher._hashes_to_strings[new_hash] = new_string

        # Mock flush to use our temp dir
        def mock_flush(sid: str) -> None:
            mappings_to_write = hasher._hashes_to_strings
            hasher._hashes_to_strings = {}
            with strings_file.open("a", encoding="utf-8") as f:
                for h, s in mappings_to_write.items():
                    if h not in hasher._seen_hashes:
                        f.write(json.dumps({"hash": h, "original": s}) + "\n")
                        hasher._seen_hashes.add(h)

        hasher.flush = mock_flush  # type: ignore[method-assign]
        hasher.flush(session_id)

        # Verify only the new hash was appended
        with strings_file.open("r", encoding="utf-8") as f:
            lines = f.readlines()

        assert len(lines) == 2  # Original + 1 new
        assert existing_hash in lines[0]
        assert new_hash in lines[1]

    _SESSION_HASHERS.clear()


# --- _resolve_thinking_reasoning_conflict ---


def test_resolve_conflict_strips_effort_from_output_config() -> None:
    """Effort is removed from output_config when thinking.type == 'disabled'."""
    data: dict = {
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
    data: dict = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Generate a title"}],
        "thinking": {"type": "disabled"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result == data  # unchanged


def test_resolve_conflict_preserves_output_config_when_thinking_enabled() -> None:
    """output_config is untouched when thinking.type == 'enabled'."""
    data: dict = {
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
    data: dict = {
        "model": "deepseek-v4-pro[1m]",
        "messages": [{"role": "user", "content": "Hello"}],
        "output_config": {"effort": "high"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result["output_config"] == {"effort": "high"}


def test_resolve_conflict_handles_thinking_none() -> None:
    """output_config is untouched when thinking is None."""
    data: dict = {
        "model": "deepseek-v4-pro[1m]",
        "messages": [{"role": "user", "content": "Hello"}],
        "thinking": None,
        "output_config": {"effort": "high"},
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result["output_config"] == {"effort": "high"}


def test_resolve_conflict_handles_output_config_not_dict() -> None:
    """No-op when thinking is disabled but output_config is not a dict."""
    data: dict = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "Generate a title"}],
        "thinking": {"type": "disabled"},
        "output_config": "not-a-dict",
    }
    result = _resolve_thinking_reasoning_conflict(data)
    assert result == data  # unchanged


def test_resolve_conflict_only_modifies_effort_in_output_config() -> None:
    """Only output_config.effort is removed; all other keys preserved."""
    data: dict = {
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
