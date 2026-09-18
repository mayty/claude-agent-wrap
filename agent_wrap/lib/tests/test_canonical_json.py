# This file has been created with the assistance of an AI tool.
"""
Tests for canonical JSON encoding and content addressing.

The golden vectors below are real values lifted from the log tree -- a message element,
a `system` block and a `tools` entry -- with the digests this encoder produced for them.
They exist because a changed encoder does not fail loudly: it silently splits dedup, so
already-stored content is written again under a new address and the database quietly
grows. A byte-level pin is the only thing that turns that into a test failure.

Note that all three carry unresolved ``hash:`` pointers. That is the contract: values are
addressed exactly as they appear in the file.
"""

import pytest

from agent_wrap.lib.canonical_json import canonical_bytes, content_address

# (label, value, expected sha256 hex) -- all three taken verbatim from the log tree.
# Annotated so the empty `properties` object below is a value, not an inferred container.
GOLDEN_VECTORS: list[tuple[str, object, str]] = [
    (
        "message",
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "hash:4163e589670c32d0419d2634993a432a7a6ad508a1b71e1fe4cb3ba949d69d2b",
                }
            ],
        },
        "605d84b29da90a15fd2e81b7324f741d97fef279f525fb905897d46f543a6ae4",
    ),
    (
        "system",
        [
            {
                "type": "text",
                "text": "hash:2abd5645dd6ab590d1d0bc3d05b17f37aa49243850ca83bf12a964a01803af5c",
            },
            {"type": "text", "text": "You are Claude Code, Anthropic's official CLI for Claude."},
            {
                "type": "text",
                "text": "hash:765b5ba2fa0a315a3c749c7e54bf5cef450084a745eb01e66371b8b6359d4752",
            },
        ],
        "fd90c64b7b07b1d955c9883247df3ac26b2a3f308c7297054d127792d17c12c7",
    ),
    (
        "tool",
        {
            "name": "DeferredToolPlaceholder",
            "description": "hash:97f0a3ed59c75538669383689dabea6e1def347a07e0d5d2f2819e13a0fa9c70",
            "input_schema": {"type": "object", "properties": {}},
            "defer_loading": True,
        },
        "29efa0dd9df9d3c458cab633a8bae68f8e6cb93f69526e6fc7e0be102322a891",
    ),
]


@pytest.mark.parametrize(("label", "value", "expected"), GOLDEN_VECTORS)
def test_golden_vectors_from_the_log_tree(label: str, value: object, expected: str) -> None:
    """A changed encoder splits dedup silently; this is what makes it fail loudly."""
    assert content_address(value).hex() == expected, f"{label} vector changed"


def test_key_order_does_not_change_the_address() -> None:
    """Object key order is not semantic in JSON, so it must not reach the address."""
    a = {"role": "user", "content": "hi", "extra": 1}
    b = {"extra": 1, "content": "hi", "role": "user"}
    assert canonical_bytes(a) == canonical_bytes(b)
    assert content_address(a) == content_address(b)


def test_nested_key_order_is_normalised_too() -> None:
    a = {"outer": {"b": 1, "a": 2}}
    b = {"outer": {"a": 2, "b": 1}}
    assert canonical_bytes(a) == canonical_bytes(b)


def test_encoding_carries_no_insignificant_whitespace() -> None:
    assert canonical_bytes({"a": 1, "b": [1, 2]}) == b'{"a":1,"b":[1,2]}'


def test_non_ascii_is_emitted_as_utf8_not_escaped() -> None:
    r"""Conversation text is heavily non-ASCII; \\uXXXX escaping would inflate it."""
    encoded = canonical_bytes({"text": "héllo ✓ 日本語"})
    assert encoded == '{"text":"héllo ✓ 日本語"}'.encode()
    assert b"\\u" not in encoded


def test_a_lone_surrogate_is_encodable_rather_than_fatal() -> None:
    """
    JSON permits unpaired surrogates and ``json.loads`` hands them straight back.

    Strict UTF-8 raises on those, which would turn one malformed model response into a
    crashed ingest pass. The encoder must be total.
    """
    value = {"text": "\ud800"}
    assert content_address(value)  # does not raise
    # Still distinguishable from the literal escape text, i.e. not silently replaced.
    assert content_address(value) != content_address({"text": "\\ud800"})


def test_hash_pointers_are_not_resolved() -> None:
    """A pointer is addressed as the string it is -- resolving it is not this layer's job."""
    pointer = "hash:" + "0" * 64
    assert canonical_bytes(pointer) == f'"{pointer}"'.encode()


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (1, 1.0),
        (1, True),
        (0, False),
        ("1", 1),
        (None, "null"),
        ([1, 2], [2, 1]),
        ({"a": 1}, {"a": "1"}),
    ],
)
def test_distinct_values_get_distinct_addresses(left: object, right: object) -> None:
    """Dedup must not merge values a consumer would tell apart."""
    assert content_address(left) != content_address(right)


def test_address_is_a_raw_32_byte_digest() -> None:
    """The column is a BLOB constrained to length 32; hex would double the index."""
    digest = content_address({"a": 1})
    assert isinstance(digest, bytes)
    assert len(digest) == 32


def test_addressing_is_stable_across_calls() -> None:
    value = {"b": [1, {"z": None, "a": "x"}], "a": 2}
    assert content_address(value) == content_address(value)
