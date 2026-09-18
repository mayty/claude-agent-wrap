# This file has been created with the assistance of an AI tool.
"""Tests for walking the central log tree into session keys."""

from typing import TYPE_CHECKING

from agent_wrap.domain.logs.ingest import discover_sessions
from agent_wrap.infrastructure.logs.models import SessionKey

if TYPE_CHECKING:
    from pathlib import Path


def _make_session(root: Path, project_hash: str, provider: str, session_id: str) -> Path:
    """Create one session directory holding a messages file, as a sidecar would."""
    session_dir = root / project_hash / provider / session_id
    session_dir.mkdir(parents=True)
    (session_dir / "messages.jsonl").write_text('{"status": "success"}\n', encoding="utf-8")
    return session_dir


def test_an_absent_root_yields_nothing(tmp_path: Path) -> None:
    """The state of a host whose sidecars have never written a log."""
    assert discover_sessions(tmp_path / "litellm-logs") == []


def test_an_empty_root_yields_nothing(tmp_path: Path) -> None:
    assert discover_sessions(tmp_path) == []


def test_a_session_is_keyed_by_its_three_path_components(tmp_path: Path) -> None:
    session_dir = _make_session(tmp_path, "abc123", "bedrock", "sess-1")

    assert discover_sessions(tmp_path) == [
        (
            SessionKey(project_hash="abc123", provider="bedrock", claude_session_id="sess-1"),
            session_dir,
        )
    ]


def test_every_project_provider_and_session_is_found(tmp_path: Path) -> None:
    """Three levels, each fanning out -- the shape the real tree has."""
    _make_session(tmp_path, "hash-a", "bedrock", "sess-1")
    _make_session(tmp_path, "hash-a", "bedrock", "sess-2")
    _make_session(tmp_path, "hash-a", "dashscope", "sess-1")
    _make_session(tmp_path, "hash-b", "bedrock", "sess-3")

    assert [key for key, _ in discover_sessions(tmp_path)] == [
        SessionKey("hash-a", "bedrock", "sess-1"),
        SessionKey("hash-a", "bedrock", "sess-2"),
        SessionKey("hash-a", "dashscope", "sess-1"),
        SessionKey("hash-b", "bedrock", "sess-3"),
    ]


def test_one_session_under_two_providers_is_two_entries(tmp_path: Path) -> None:
    """
    A session that switched provider mid-flight has one directory per provider.

    Merging them back into the single session a user sees is read-time policy, so the
    walk must keep them apart -- collapsing them here would make the two ingest passes
    fight over one row.
    """
    _make_session(tmp_path, "hash-a", "bedrock", "shared-id")
    _make_session(tmp_path, "hash-a", "deepseek", "shared-id")

    assert [key.provider for key, _ in discover_sessions(tmp_path)] == ["bedrock", "deepseek"]


def test_a_directory_without_a_messages_file_is_skipped(tmp_path: Path) -> None:
    """
    A session dir the sidecar created but never wrote to is not a session yet.

    It is skipped rather than ingested as empty, so that it is picked up the moment it
    does get a record instead of occupying a row that says nothing.
    """
    (tmp_path / "hash-a" / "bedrock" / "empty").mkdir(parents=True)
    _make_session(tmp_path, "hash-a", "bedrock", "real")

    assert [key.claude_session_id for key, _ in discover_sessions(tmp_path)] == ["real"]


def test_files_at_each_level_are_ignored(tmp_path: Path) -> None:
    """Only directories are descended into; a stray file at any depth is not a project."""
    _make_session(tmp_path, "hash-a", "bedrock", "sess-1")
    (tmp_path / "stray.json").write_text("{}", encoding="utf-8")
    (tmp_path / "hash-a" / "stray.json").write_text("{}", encoding="utf-8")
    (tmp_path / "hash-a" / "bedrock" / "stray.json").write_text("{}", encoding="utf-8")

    assert len(discover_sessions(tmp_path)) == 1


def test_a_session_nested_deeper_than_three_levels_is_not_found(tmp_path: Path) -> None:
    """
    The walk is three fixed levels, not a recursive search.

    Anything deeper is not a session the sidecar wrote, and treating it as one would
    key it by whatever directory names happened to sit at those depths.
    """
    deep = tmp_path / "hash-a" / "bedrock" / "sess-1" / "nested"
    deep.mkdir(parents=True)
    (deep / "messages.jsonl").write_text("{}\n", encoding="utf-8")

    assert discover_sessions(tmp_path) == []


def test_an_unreadable_directory_is_skipped_rather_than_raising(tmp_path: Path) -> None:
    """
    A directory that cannot be listed is a session that no longer exists to us.

    The walk runs against a tree three sidecars are writing to concurrently, so a dir
    vanishing or losing its permissions mid-walk is expected rather than exceptional --
    and the alternative, raising, would abandon every session after it.
    """
    _make_session(tmp_path, "hash-a", "bedrock", "sess-1")
    blocked = tmp_path / "hash-b" / "bedrock"
    blocked.mkdir(parents=True)
    blocked.chmod(0o000)
    try:
        assert [key.project_hash for key, _ in discover_sessions(tmp_path)] == ["hash-a"]
    finally:
        # Restored unconditionally: pytest's tmp_path teardown cannot remove a 000 dir.
        blocked.chmod(0o700)


def test_the_result_is_sorted_regardless_of_creation_order(tmp_path: Path) -> None:
    """
    Determinism, so two backfills report the same order and progress is reproducible.

    ``iterdir`` returns directory order, which is neither sorted nor stable across
    filesystems, so the sort is what makes the walk's output a function of the tree.
    """
    for session_id in ("sess-9", "sess-1", "sess-5"):
        _make_session(tmp_path, "hash-a", "bedrock", session_id)

    keys = [key.claude_session_id for key, _ in discover_sessions(tmp_path)]
    assert keys == sorted(keys)
