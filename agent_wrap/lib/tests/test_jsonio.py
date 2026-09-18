# This file has been created with the assistance of an AI tool.
"""Tests for the tolerant JSON readers."""

from typing import TYPE_CHECKING

import pytest

from agent_wrap.lib.jsonio import json_array, json_object, read_json_object

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ("", {}),
        ("  \n ", {}),
        # Everything below is the "leave it alone" answer: a caller must not overwrite it.
        ("{bad json", None),
        ("[1, 2]", None),
        ('"a string"', None),
    ],
)
def test_read_json_object(tmp_path: Path, text: str, expected: dict[str, int] | None) -> None:
    path = tmp_path / "x.json"
    path.write_text(text)
    assert read_json_object(path) == expected


def test_read_json_object_reads_a_missing_file_as_none(tmp_path: Path) -> None:
    assert read_json_object(tmp_path / "absent.json") is None


@pytest.mark.parametrize("raw", ["", "not json", "[1]", "null", "7"])
def test_json_object_falls_back_to_empty(raw: str) -> None:
    assert json_object(raw) == {}


@pytest.mark.parametrize("raw", ["", "not json", '{"a": 1}', "null", "7"])
def test_json_array_falls_back_to_empty(raw: str) -> None:
    assert json_array(raw) == []
