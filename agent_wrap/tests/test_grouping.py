# This file has been created with the assistance of an AI tool.
"""Tests for the `.agent_stats_leaf` transient-project grouping resolver."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.lib.grouping import MARKER_NAME, resolve_group

if TYPE_CHECKING:
    from pathlib import Path


def _marker(directory: Path, contents: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / MARKER_NAME).write_text(contents, encoding="utf-8")


def test_no_marker_keeps_project_standalone(tmp_path: Path):
    proj = tmp_path / "solo"
    proj.mkdir()
    root, name, custom = resolve_group(proj)
    assert root == proj
    assert name == "solo"
    assert custom is False


def test_marker_with_content_uses_first_nonempty_line(tmp_path: Path):
    runs = tmp_path / "runs"
    _marker(runs, "\n  batch-feb \nignored second line\n")
    child = runs / "agent-xyz"
    child.mkdir()
    root, name, custom = resolve_group(child)
    assert root == runs
    assert name == "batch-feb"
    assert custom is True


def test_empty_marker_falls_back_to_dir_name(tmp_path: Path):
    runs = tmp_path / "runs"
    _marker(runs, "   \n\n")
    child = runs / "agent-xyz"
    child.mkdir()
    root, name, custom = resolve_group(child)
    assert root == runs
    assert name == "runs"
    assert custom is False


def test_marker_on_project_itself_is_found(tmp_path: Path):
    proj = tmp_path / "proj"
    _marker(proj, "self-named")
    root, name, custom = resolve_group(proj)
    assert root == proj
    assert name == "self-named"
    assert custom is True


def test_nearest_marker_wins_when_nested(tmp_path: Path):
    outer = tmp_path / "outer"
    _marker(outer, "outer-group")
    inner = outer / "inner"
    _marker(inner, "inner-group")
    leaf = inner / "agent-1"
    leaf.mkdir()
    root, name, custom = resolve_group(leaf)
    assert root == inner
    assert name == "inner-group"
    assert custom is True


def test_two_projects_under_one_marker_share_a_group(tmp_path: Path):
    runs = tmp_path / "runs"
    _marker(runs, "batch")
    a = runs / "a"
    b = runs / "b"
    a.mkdir()
    b.mkdir()
    root_a, name_a, _ = resolve_group(a)
    root_b, name_b, _ = resolve_group(b)
    assert root_a == root_b == runs
    assert name_a == name_b == "batch"


def test_symlinked_projects_group_by_literal_path(tmp_path: Path):
    # Two physically-separate projects, symlinked into one marked common dir.
    # Grouping must follow the *literal* (symlinked) path so they aggregate,
    # even though their resolved physical locations differ.
    real_a = tmp_path / "real" / "alpha"
    real_b = tmp_path / "real" / "beta"
    real_a.mkdir(parents=True)
    real_b.mkdir(parents=True)

    common = tmp_path / "common"
    _marker(common, "fleet")
    (common / "alpha").symlink_to(real_a, target_is_directory=True)
    (common / "beta").symlink_to(real_b, target_is_directory=True)

    root_a, name_a, custom_a = resolve_group(common / "alpha")
    root_b, name_b, custom_b = resolve_group(common / "beta")

    # Both literal paths resolve to the same common group root and name...
    assert root_a == root_b == common
    assert name_a == name_b == "fleet"
    assert custom_a is custom_b is True
    # ...even though their physical (resolved) paths are distinct.
    assert real_a.resolve() != real_b.resolve()
