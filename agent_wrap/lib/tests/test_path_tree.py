# This file has been created with the assistance of an AI tool.
"""Tests for the generic path trie and its tree glyphs."""

from agent_wrap.lib.path_tree import (
    PathTreeNode,
    build_path_tree,
    expand_widest_chain,
    walk_path_tree,
)


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


def _chopped(paths: list[str], times: int) -> list[str]:
    """Walk a tree over *paths* after *times* rounds of chopping."""
    root = build_path_tree([(p, p) for p in paths])
    for _ in range(times):
        expand_widest_chain(root)
    return [line.label for line in walk_path_tree(root)]


def test_expand_widest_chain_splits_the_leading_segment_off() -> None:
    """The fold `_compress` made is given back one segment at a time."""
    assert _chopped(["/home/me/wotp"], 1) == ["└home/", " └me/wotp"]


def test_expand_widest_chain_narrows_the_widest_line() -> None:
    """The point of the split: the column the table has to reserve gets smaller."""
    before = max(len(label) for label in _chopped(["/home/me/work/wotp"], 0))
    after = max(len(label) for label in _chopped(["/home/me/work/wotp"], 1))
    assert (before, after) == (18, 14)


def test_expand_widest_chain_splits_the_whole_sibling_group() -> None:
    """
    The widest chain decides *when*; every folded sibling then gives up a segment too.

    Note what moving together buys: both were leaves and sorted `home`, `srv`, and both are
    subtree nodes afterwards, so they sort `home`, `srv` still. Splitting only the wide one
    would have left `srv/app` a leaf and sent it above.
    """
    assert _chopped(["/srv/app", "/home/me/work/wotp"], 0) == ["├home/me/work/wotp", "└srv/app"]
    assert _chopped(["/srv/app", "/home/me/work/wotp"], 1) == [
        "├home/",
        "│└me/work/wotp",
        "└srv/",
        " └app",
    ]


def test_expand_widest_chain_leaves_a_sibling_that_is_already_one_segment() -> None:
    """`etc` has nothing to give, so the group split simply passes it by."""
    assert _chopped(["/etc", "/home/me/work/wotp"], 1) == [
        "├etc",
        "└home/",
        " └me/work/wotp",
    ]


def test_expand_widest_chain_keeps_sibling_order_across_a_split() -> None:
    """
    Splitting one sibling of a group would reorder the group around it.

    A split node stops being a leaf, and `walk_path_tree` sorts leaves before subtree
    nodes -- so chopping only the widest would send its unsplit siblings *above* it. The
    group moves together, and the order it had is the order it keeps.
    """
    paths = ["/wot/feature_branch/game/res", "/wot/stable/game/res", "/wot/trunk/game"]
    assert _chopped(paths, 1) == [
        "└wot/",
        " ├feature_branch/",
        " │└game/res",
        " ├stable/",
        " │└game/res",
        " └trunk/",
        "  └game",
    ]


def test_expand_widest_chain_reports_when_nothing_is_folded() -> None:
    root = build_path_tree([("/home/me/wotp", "row")])
    assert [expand_widest_chain(root) for _ in range(3)] == [True, True, False]


def test_expand_widest_chain_fully_uncompresses_when_run_to_exhaustion() -> None:
    """Called until it says no, every line is one path segment."""
    root = build_path_tree([(p, p) for p in ["/home/me/a", "/home/me/b"]])
    while expand_widest_chain(root):
        pass
    assert [line.label for line in walk_path_tree(root)] == ["└home/", " └me/", "  ├a", "  └b"]


def test_expand_widest_chain_keeps_a_shared_prefix_stated_once() -> None:
    """Chopping splits segments apart; it never repeats one on a sibling's line."""
    assert _chopped(["/home/me/a", "/home/me/b"], 1) == ["└home/", " └me/", "  ├a", "  └b"]


def test_expand_widest_chain_carries_the_row_count_onto_the_new_stem() -> None:
    """The stem stands for the same subtree, so it reports the same count."""
    root = build_path_tree([(p, p) for p in ["/home/me/a", "/home/me/b"]])
    expand_widest_chain(root)
    assert root.children["home"].subtree_row_count == 2


def test_expand_widest_chain_leaves_the_new_stem_structural() -> None:
    """A stem is a heading, never a row of its own."""
    root = build_path_tree([("/home/me/wotp", "row")])
    expand_widest_chain(root)
    assert root.children["home"].row is None


def test_expand_widest_chain_copies_a_subclasss_aggregates_onto_the_stem() -> None:
    """A caller's extra per-node state comes along, including a slot filled after the build."""

    class Counted(PathTreeNode[str]):
        __slots__ = ("depth_hint", "unset_until_later")

        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.depth_hint = 0
            self.unset_until_later: str

    root = build_path_tree([("/home/me/wotp", "row")], node_factory=Counted)
    folded = root.children["home/me/wotp"]
    assert isinstance(folded, Counted)
    folded.depth_hint = 7
    folded.unset_until_later = "filled"

    expand_widest_chain(root)
    stem = root.children["home"]
    assert isinstance(stem, Counted)
    assert (stem.depth_hint, stem.unset_until_later) == (7, "filled")


def test_expand_widest_chain_tolerates_a_subclass_slot_that_was_never_assigned() -> None:
    """An annotated-but-unfilled slot is skipped rather than raising."""

    class Deferred(PathTreeNode[str]):
        __slots__ = ("filled_by_a_later_pass",)

        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.filled_by_a_later_pass: str

    root = build_path_tree([("/home/me/wotp", "row")], node_factory=Deferred)
    assert expand_widest_chain(root) is True
    assert not hasattr(root.children["home"], "filled_by_a_later_pass")


def test_expand_widest_chain_refuses_a_split_that_would_widen_the_tree() -> None:
    """
    Splitting `home/me/` indents `wotp-be` one further, and that leaf already set the width.

    The tree is left exactly as it was, and the caller is told there is nothing more to do.
    """
    root = build_path_tree([(p, p) for p in ["/home/me/wotp", "/home/me/wotp-be"]])
    assert expand_widest_chain(root) is False
    assert [line.label for line in walk_path_tree(root)] == ["└home/me/", " ├wotp", " └wotp-be"]


def test_expand_widest_chain_splits_chains_that_are_tied_for_widest() -> None:
    """Neither split helps alone, so refusing the first would stall the pair."""
    root = build_path_tree([(p, p) for p in ["/aa/bb/one", "/cc/dd/two"]])
    while expand_widest_chain(root):
        pass
    assert [line.label for line in walk_path_tree(root)] == [
        "├aa/",
        "│└bb/",
        "│ └one",
        "└cc/",
        " └dd/",
        "  └two",
    ]
