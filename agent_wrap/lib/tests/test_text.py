# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.lib.text."""

import pytest

from agent_wrap.lib.text import mask_value, repetition_count

#: Values that are some number of copies of a shorter unit.
REPEATED = ["aa", "abcabc", "abcabcabc", "aabaab", "ab" * 50, "sk-live-9f" * 2, "é" * 3]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 1),
        ("a", 1),
        ("aa", 2),
        ("aaa", 3),
        ("abcd", 1),
        ("abcab", 1),
        ("abcabc", 2),
        ("abcabcabc", 3),
        ("aabaab", 2),
        ("ab" * 50, 50),
        ("a" * 1000, 1000),
        ("é" * 3, 3),
    ],
)
def test_repetition_count(text: str, expected: int) -> None:
    assert repetition_count(text) == expected


@pytest.mark.parametrize("text", REPEATED)
def test_repetition_unit_reconstructs_the_input(text: str) -> None:
    """The caller slices the unit off the front, so the division must be exact."""
    repeats = repetition_count(text)
    assert text[: len(text) // repeats] * repeats == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", "***"),
        ("short", "***"),
        ("1234567", "***"),
        ("12345678", "12***78"),
        ("sk-1234567890abcdef", "sk***ef"),
        ("aGVsbG8gd29ybGQ=", "aG***GQ="),
        ("aGVsbG8gd29ybGQh==", "aG***Qh=="),
        ("пароль-секретный-длинный", "па***ый"),
    ],
)
def test_mask_value(text: str, expected: str) -> None:
    assert mask_value(text) == expected


@pytest.mark.parametrize("text", ["====", "=" * 40, "ab" + "=" * 20, "=="])
def test_mask_value_hides_a_run_of_equals_too_long_to_be_padding(text: str) -> None:
    """Re-appending the run verbatim would print the whole of an all-'=' value."""
    assert mask_value(text) == "***"


@pytest.mark.parametrize(
    "text", ["12345678", "sk-1234567890abcdef", "aGVsbG8gd29ybGQ=", "пароль-секретный-длинный"]
)
def test_mask_value_never_shows_the_middle(text: str) -> None:
    core = text.rstrip("=")
    assert core[2:-2] not in mask_value(text)


@pytest.mark.parametrize("text", ["", "short", "12345678", "sk-1234567890abcdef", "===="])
def test_mask_value_reveals_at_most_four_characters(text: str) -> None:
    revealed = sum(1 for char in mask_value(text) if char not in "*=")
    assert revealed <= 4
