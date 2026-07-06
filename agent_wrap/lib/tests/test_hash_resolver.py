from __future__ import annotations

from agent_wrap.lib.hash_resolver import resolve_hashes

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
