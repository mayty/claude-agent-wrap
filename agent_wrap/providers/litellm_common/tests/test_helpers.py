# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/helpers.py."""

from __future__ import annotations

from agent_wrap.providers.litellm_common.helpers import (
    get_response_content_str,
    json_safe,
)
from agent_wrap.providers.litellm_common.string_hasher import StringHasher

# ---------------------------------------------------------------------------
# json_safe
# ---------------------------------------------------------------------------


def test_json_safe_primitive_none() -> None:
    """None passes through unchanged."""
    assert json_safe(None) is None


def test_json_safe_primitive_int_float_bool() -> None:
    """Primitive types pass through unchanged."""
    assert json_safe(42) == 42
    assert json_safe(3.14) == 3.14
    assert json_safe(True) is True  # noqa: FBT003
    assert json_safe(False) is False  # noqa: FBT003
    assert json_safe(0) == 0
    assert json_safe(-1) == -1


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


# ---------------------------------------------------------------------------
# get_response_content_str
# ---------------------------------------------------------------------------


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
