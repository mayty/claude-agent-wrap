# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/lib/atomic.py."""

from __future__ import annotations

import json
from pathlib import Path

from agent_wrap.lib.atomic import atomic_write_json, atomic_write_text

# --- atomic_write_text ---


def test_write_text_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "hello")
    assert target.read_text() == "hello"


def test_write_text_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    target.write_text("old")
    atomic_write_text(target, "new")
    assert target.read_text() == "new"


def test_write_text_creates_parent_dirs(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "deep" / "out.txt"
    atomic_write_text(target, "x")
    assert target.read_text() == "x"


def test_write_text_leaves_no_tmp_file(tmp_path: Path) -> None:
    target = tmp_path / "out.txt"
    atomic_write_text(target, "x")
    assert list(tmp_path.iterdir()) == [target]


# --- atomic_write_json ---


def test_write_json_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    data = {"a": 1, "b": ["x", "y"]}
    atomic_write_json(target, data)
    assert json.loads(target.read_text()) == data


def test_write_json_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "out.json"
    atomic_write_json(target, {"k": "v"})
    assert target.read_text().endswith("}\n")
