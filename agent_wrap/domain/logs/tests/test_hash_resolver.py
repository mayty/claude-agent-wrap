# This file has been edited with the assistance of an AI tool.
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agent_wrap.domain.logs.hash_resolver import load_strings, resolve_hashes

if TYPE_CHECKING:
    from pathlib import Path

# --- resolve_hashes ---


def test_resolve_hashes_replaces_known_hashes():
    obj = {"msg": "hash:abc123", "nested": ["hash:def456"]}
    strings = {"hash:abc123": "hello", "hash:def456": "world"}
    result = resolve_hashes(obj, strings)
    assert result == {"msg": "hello", "nested": ["world"]}


def test_resolve_hashes_leaves_unknown_hashes():
    obj = {"msg": "hash:unknown"}
    strings = {"hash:abc123": "hello"}
    result = resolve_hashes(obj, strings)
    assert result == {"msg": "hash:unknown"}


def test_resolve_hashes_leaves_primitives_unchanged():
    assert resolve_hashes(None, {}) is None
    assert resolve_hashes(42, {}) == 42
    assert resolve_hashes(True, {}) is True  # noqa: FBT003
    assert resolve_hashes("plain string", {}) == "plain string"


# --- load_strings ---


def test_load_strings_round_trip(tmp_path: Path):
    sdir = tmp_path / "s"
    sdir.mkdir()
    (sdir / "strings.jsonl").write_text(
        json.dumps({"hash": "hash:a", "original": "A"})
        + "\n"
        + "not json\n"
        + json.dumps({"hash": "hash:b", "original": "B"})
        + "\n",
        encoding="utf-8",
    )
    assert load_strings(sdir) == {"hash:a": "A", "hash:b": "B"}
