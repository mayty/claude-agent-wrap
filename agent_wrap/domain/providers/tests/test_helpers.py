# This file has been created with the assistance of an AI tool.
"""Tests for providers/litellm_runtime/helpers.py."""

import importlib.util
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import override

import pytest

_RUNTIME_DIR = Path(__file__).parent.parent / "litellm_runtime"


def _import_runtime_module(name: str):
    """
    Import a module from the (non-package) litellm_runtime/ directory.

    Modules are registered in ``sys.modules`` so that internal imports
    (e.g. ``helpers.py`` doing ``from string_hasher import ...``) resolve
    to the same module object — no duplicate module state.
    """
    spec = importlib.util.spec_from_file_location(name, _RUNTIME_DIR / f"{name}.py")
    assert spec is not None, f"Could not find {_RUNTIME_DIR / f'{name}.py'}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # pyrefly: ignore [missing-attribute]
    return mod


_helpers = _import_runtime_module("helpers")
# string_hasher was loaded as a side effect of loading helpers
# (helpers.py does ``from string_hasher import StringHasher``).
_string_hasher = sys.modules["string_hasher"]

get_response_content_str = _helpers.get_response_content_str
json_safe = _helpers.json_safe
REDACTED_VALUE = _helpers.REDACTED_VALUE
StringHasher = _string_hasher.StringHasher


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (42, 42),
        (3.14, 3.14),
        (True, True),
        (False, False),
        (0, 0),
        (-1, -1),
    ],
)
def test_json_safe_primitives_pass_through(value: object, expected: object) -> None:
    """Primitive types (and None) pass through unchanged."""
    assert json_safe(value) == expected


def test_json_safe_string_short() -> None:
    """Short strings pass through unchanged."""
    assert json_safe("hello") == "hello"
    assert json_safe("") == ""


def test_json_safe_string_long_without_hasher() -> None:
    """Long strings pass through unchanged when no hasher is provided."""
    long_str = "a" * 100
    assert json_safe(long_str) == long_str


def test_json_safe_string_long_with_hasher() -> None:
    """Long strings are hashed when a hasher is provided."""
    hasher = StringHasher()
    long_str = "b" * 100
    result = json_safe(long_str, _hasher=hasher)
    assert result.startswith("hash:")
    assert len(result) == 69  # "hash:" (5) + 64 hex chars


def test_json_safe_dict_keys_as_strings() -> None:
    """Non-string dict keys are converted to strings."""
    result = json_safe({1: "one", 2: "two"})
    assert result == {"1": "one", "2": "two"}
    assert all(isinstance(k, str) for k in result)


def test_json_safe_dict_recursive_values() -> None:
    """Dict values are recursively coerced."""
    result = json_safe({"nested": {"a": 1, "b": [2, 3]}})
    assert result == {"nested": {"a": 1, "b": [2, 3]}}


def test_json_safe_dict_hashes_long_strings() -> None:
    """Nested long strings in dict values are hashed."""
    hasher = StringHasher()
    long_str = "c" * 100
    result = json_safe({"key": long_str}, _hasher=hasher)
    assert result["key"].startswith("hash:")


def test_json_safe_list() -> None:
    """List elements are recursively coerced."""
    assert json_safe([1, "two", 3.0]) == [1, "two", 3.0]


def test_json_safe_tuple() -> None:
    """Tuple is converted to a list for JSON compatibility."""
    result = json_safe((1, 2, 3))
    assert result == [1, 2, 3]
    assert isinstance(result, list)


def test_json_safe_set() -> None:
    """Set is converted to a list for JSON compatibility."""
    result = json_safe({3, 1, 2})
    assert sorted(result) == [1, 2, 3]
    assert isinstance(result, list)


def test_json_safe_model_dump_method() -> None:
    """Object with model_dump() is serialized via that method."""

    class WithModelDump:
        def model_dump(self) -> dict[str, object]:
            return {"kind": "model_dump", "value": 42}

    result = json_safe(WithModelDump())
    assert result == {"kind": "model_dump", "value": 42}


def test_json_safe_dict_method() -> None:
    """Object with dict() is serialized via that method."""

    class WithDict:
        def dict(self) -> dict[str, object]:
            return {"kind": "dict", "value": 99}

    result = json_safe(WithDict())
    assert result == {"kind": "dict", "value": 99}


def test_json_safe_model_dump_preferred_over_dict() -> None:
    """model_dump() is preferred over dict() when both exist."""

    class Both:
        def model_dump(self) -> dict[str, object]:
            return {"source": "model_dump"}

        def dict(self) -> dict[str, object]:
            return {"source": "dict"}

    result = json_safe(Both())
    assert result == {"source": "model_dump"}


def test_json_safe_model_dump_fallback_to_str() -> None:
    """When model_dump() raises, fallback is str() not dict()."""

    class RaisesOnModelDump:
        def model_dump(self) -> dict[str, object]:
            msg = "boom"
            raise ValueError(msg)

        def __str__(self) -> str:
            return "fallback-string"

    result = json_safe(RaisesOnModelDump())
    assert result == "fallback-string"


def test_json_safe_unknown_type_fallback_to_str() -> None:
    """Object without model_dump() or dict() falls back to str()."""

    class Custom:
        def __str__(self) -> str:
            return "custom-repr"

    assert json_safe(Custom()) == "custom-repr"


def test_json_safe_unknown_type_hashed_with_hasher() -> None:
    """str() fallback for unknown types is hashed when hasher is provided."""

    class LongStr:
        def __str__(self) -> str:
            return "x" * 100

    hasher = StringHasher()
    result = json_safe(LongStr(), _hasher=hasher)
    assert result.startswith("hash:")
    assert len(result) == 69


def test_json_safe_cycle_detection() -> None:
    """Self-referencing dict returns '<recursive_record>' for the cycle."""
    d: dict[str, object] = {}
    d["self"] = d
    result = json_safe(d)
    assert result == {"self": "<recursive_record>"}


def test_json_safe_cycle_detection_in_list() -> None:
    """A list that contains itself returns '<recursive_record>' for the cycle."""
    lst: list[object] = []
    lst.append(lst)
    result = json_safe(lst)
    assert result == ["<recursive_record>"]


def test_json_safe_cycle_detection_nested() -> None:
    """Cycle multiple levels deep (dict → list → dict → original) is detected."""
    d: dict[str, object] = {}
    lst: list[object] = [d]
    d["items"] = lst
    result = json_safe(d)
    assert result == {"items": ["<recursive_record>"]}


def test_json_safe_shared_references_serialized_normally() -> None:
    """Shared references are duplicated, not marked as cycles."""
    inner = {"key": "value"}
    d: dict[str, object] = {"a": inner, "b": inner}
    result = json_safe(d)
    assert result == {"a": {"key": "value"}, "b": {"key": "value"}}


def test_json_safe_empty_containers() -> None:
    """Empty containers serialize correctly."""
    assert json_safe({}) == {}
    assert json_safe([]) == []
    assert json_safe(()) == []
    assert json_safe(set()) == []


def test_json_safe_non_dict_mapping_serialized_as_object() -> None:
    """
    A Mapping that isn't a dict serializes as an object, not str().

    LiteLLM's /anthropic/* passthrough route puts a Starlette ``Headers`` mapping in
    proxy_server_request["headers"]; when that hit the str() fallback the whole
    header set became one opaque blob and the logs viewer lost the
    x-claude-code-agent-id it splits subagent threads on.
    """

    class FakeHeaders(Mapping[str, str]):
        """Minimal stand-in for Starlette's Headers: a Mapping that is not a dict."""

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

    headers = FakeHeaders({"accept": "application/json", "x-claude-code-agent-id": "a52736f97"})
    assert json_safe({"headers": headers}) == {
        "headers": {"accept": "application/json", "x-claude-code-agent-id": "a52736f97"}
    }


def test_json_safe_redacts_credential_keys() -> None:
    """Credential-bearing keys are replaced, and matched case-insensitively."""
    result = json_safe({"Authorization": "Bearer sk-ant-oat01-secret", "accept": "text/plain"})
    assert result == {"Authorization": REDACTED_VALUE, "accept": "text/plain"}


def test_json_safe_redacts_nested_credential_keys() -> None:
    """
    Redaction reaches nested headers, not just the top-level ones.

    LiteLLM's router routes repeat the client's headers inside
    ``body.secret_fields.raw_headers``, so the master key is inline there too.
    """
    record = {"body": {"secret_fields": {"raw_headers": {"authorization": "Bearer sk-aw-key"}}}}
    result = json_safe(record)
    assert result["body"]["secret_fields"]["raw_headers"]["authorization"] == REDACTED_VALUE


def test_json_safe_redacted_credential_is_not_interned(tmp_path: Path) -> None:
    """A long credential is replaced outright, never interned into strings.jsonl."""
    hasher = StringHasher()
    result = json_safe({"authorization": "Bearer " + "s" * 200}, _hasher=hasher)
    assert result == {"authorization": REDACTED_VALUE}
    hasher.flush(tmp_path)
    assert not (tmp_path / "strings.jsonl").exists()


def test_json_safe_does_not_mutate_source_mapping() -> None:
    """Serialization builds new containers; the caller's live structure is untouched."""
    headers = {"authorization": "Bearer sk-ant-oat01-secret"}
    json_safe({"headers": headers})
    assert headers == {"authorization": "Bearer sk-ant-oat01-secret"}


def test_get_response_content_str_message_content() -> None:
    """Extracts text from choices[0].message.content."""
    response = {"choices": [{"message": {"content": "Hello world"}}]}
    assert get_response_content_str(response) == "Hello world"


def test_get_response_content_str_text_fallback() -> None:
    """Falls back to choices[0].text when no message.content."""
    response = {"choices": [{"text": "Fallback text"}]}
    assert get_response_content_str(response) == "Fallback text"


def test_get_response_content_str_message_preferred_over_text() -> None:
    """message.content is preferred over text when both are present."""
    response = {"choices": [{"message": {"content": "Message content"}, "text": "Text fallback"}]}
    assert get_response_content_str(response) == "Message content"


def test_get_response_content_str_non_dict_input() -> None:
    """Returns None for non-dict inputs."""
    assert get_response_content_str("string") is None
    assert get_response_content_str(42) is None
    assert get_response_content_str(None) is None
    assert get_response_content_str(["not", "a", "dict"]) is None


def test_get_response_content_str_missing_choices() -> None:
    """Returns None when choices key is missing."""
    assert get_response_content_str({}) is None
    assert get_response_content_str({"no_choices_here": []}) is None


def test_get_response_content_str_choices_not_list() -> None:
    """Returns None when choices is not a list."""
    assert get_response_content_str({"choices": "not-a-list"}) is None
    assert get_response_content_str({"choices": None}) is None


def test_get_response_content_str_empty_choices() -> None:
    """Returns None when choices is an empty list."""
    assert get_response_content_str({"choices": []}) is None


def test_get_response_content_str_first_choice_not_dict() -> None:
    """Returns None when the first choice is not a dict."""
    assert get_response_content_str({"choices": ["a-string"]}) is None
    assert get_response_content_str({"choices": [42]}) is None
    assert get_response_content_str({"choices": [None]}) is None


def test_get_response_content_str_no_message_content_or_text() -> None:
    """Returns None when neither message.content nor text are present or string."""
    # Message exists but content is missing
    assert get_response_content_str({"choices": [{"message": {"role": "assistant"}}]}) is None
    # Message exists but content is not a string
    assert get_response_content_str({"choices": [{"message": {"content": 123}}]}) is None
    # No message and no text at all
    assert get_response_content_str({"choices": [{"role": "assistant"}]}) is None
