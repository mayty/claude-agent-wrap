# This file has been created with the assistance of an AI tool.
"""Tests for the generic path trie and its tree glyphs."""

from agent_wrap.lib.path_tree import PathTreeNode, build_path_tree, walk_path_tree


def _labels(paths: list[str]) -> list[str]:
    """Walk a tree built over *paths* (each its own row) and return the labels."""
    return [line.label for line in walk_path_tree(build_path_tree([(p, p) for p in paths]))]


def test_build_path_tree_names_the_root_for_the_filesystem_root() -> None:
    assert build_path_tree([("/home/me/wotp", "row")]).name == "/"


def test_build_path_tree_compresses_a_chain_nothing_branches_on() -> None:
    """A prefix no row branches on costs one line, not one per segment."""
    assert _labels(["/home/me/wotp"]) == ["└home/me/wotp"]


def test_build_path_tree_states_a_shared_prefix_once() -> None:
    assert _labels(["/home/me/a", "/home/me/b"]) == ["└home/me/", " ├a", " └b"]


def test_build_path_tree_splits_a_row_that_also_has_children() -> None:
    """The parent's own row moves to a synthetic `.` child so the parent can be a heading."""
    assert _labels(["/srv/app", "/srv/app/sub"]) == ["└srv/app/", " ├.", " └sub"]


def test_walk_path_tree_pins_the_self_row_above_its_siblings() -> None:
    """`.` stands for the parent directory itself, so it belongs directly under it."""
    labels = _labels(["/srv/app", "/srv/app/a", "/srv/app/b"])
    assert labels == ["└srv/app/", " ├.", " ├a", " └b"]


def test_walk_path_tree_orders_leaves_alphabetically() -> None:
    assert _labels(["/srv/c", "/srv/a", "/srv/b"]) == ["└srv/", " ├a", " ├b", " └c"]


def test_walk_path_tree_sinks_the_bushiest_subtree_to_the_bottom() -> None:
    """A reader scanning from the top meets the single lines first."""
    labels = _labels(["/srv/big/a", "/srv/big/b", "/srv/small/only"])
    assert labels == ["└srv/", " ├small/only", " └big/", "  ├a", "  └b"]


def test_walk_path_tree_draws_a_continuation_bar_under_a_branching_parent() -> None:
    labels = _labels(["/a/x", "/a/y", "/b/p", "/b/q"])
    assert labels == ["├a/", "│├x", "│└y", "└b/", " ├p", " └q"]


def test_walk_path_tree_reports_the_prefix_length_of_each_line() -> None:
    """The renderer needs it to leave the glyphs unstyled while colouring the text."""
    lines = walk_path_tree(build_path_tree([("/a/x", "x"), ("/a/y", "y"), ("/b", "b")]))
    assert [(line.label, line.prefix_len) for line in lines] == [
        ("├b", 1),
        ("└a/", 1),
        (" ├x", 2),
        (" └y", 2),
    ]


def test_walk_path_tree_carries_the_row_on_leaves_only() -> None:
    lines = walk_path_tree(build_path_tree([("/srv/a", "row-a"), ("/srv/b", "row-b")]))
    assert [(line.label, line.node.row) for line in lines] == [
        ("└srv/", None),
        (" ├a", "row-a"),
        (" └b", "row-b"),
    ]


def test_build_path_tree_counts_the_rows_under_every_node() -> None:
    root = build_path_tree([("/srv/a", "a"), ("/srv/b", "b"), ("/opt/c", "c")])
    assert root.subtree_row_count == 3
    assert root.children["srv"].subtree_row_count == 2


def test_build_path_tree_skips_a_path_with_no_segments() -> None:
    """It names no node, and making it the root's row would put an unnamed line on top."""
    root = build_path_tree([("", "nowhere"), ("/srv/a", "a")])
    assert root.row is None
    assert _labels(["", "/srv/a"]) == ["└srv/a"]


def test_build_path_tree_hangs_a_relative_path_off_the_root() -> None:
    assert _labels(["srv/a"]) == ["└srv/a"]


def test_build_path_tree_uses_the_node_factory_throughout() -> None:
    """A caller carrying extra per-node state must never meet a plain node."""

    class Counted(PathTreeNode[str]):
        __slots__ = ()

    root = build_path_tree([("/srv/app", "self"), ("/srv/app/sub", "child")], node_factory=Counted)
    seen = [root]
    while seen:
        node = seen.pop()
        assert isinstance(node, Counted)
        seen.extend(node.children.values())
