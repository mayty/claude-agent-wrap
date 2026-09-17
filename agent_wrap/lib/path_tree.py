# This file has been created with the assistance of an AI tool.
"""
A path trie, and the ``├``/``└`` glyphs that render one as a table column.

Generic over the payload each path carries, and deliberately ignorant of what that
payload means: everything here is decided by the *shape* of the paths alone, so the
same machinery serves a column of project directories, of ``provider/model`` keys, or
of anything else that reads as a path. Callers turn the lines it yields into their own
rows and decide what the other columns say.

Three normalizations happen between the raw trie and what is walked, and each exists
because the unnormalized tree renders badly:

* A structural node with exactly one child is folded into it (``home`` + ``me`` become
  one ``home/me`` node), so a deep prefix nobody branches on costs one line, not one
  per segment.
* A node that carries a row *and* has children is split: its own row moves to a
  synthetic ``.`` child, leaving the node structural. Without it a line would have to
  be both a leaf and a heading.
* Rows are counted per subtree, which is what orders siblings: the bushiest group sinks
  to the bottom, where a reader scanning from the top meets single lines first.

The first of those folds is reversible, because it is the one that can make a table *worse*:
a fleet of projects under a single parent folds into one very wide node, and the tree saves
almost nothing. ``expand_widest_chain`` undoes it a segment at a time, trading a line of
height for a column of width. Reversal is driven by the caller rather than decided here,
since how much width is available depends on columns this module knows nothing about.

The label a walk yields is the tree prefix plus the node's own name, and nothing else.
Suffixes that qualify the *row* rather than the path -- a "(missing)" marker, say --
are the caller's to append, since only the caller knows what its payload means.

A caller that needs more per-node state than a row -- subtree totals, say -- subclasses
``PathTreeNode`` with the extra slots and hands the subclass in as ``node_factory``.
That keeps the aggregate on the node it describes, which is where the walk's consumer
reads it, rather than in a lookup table beside the tree.
"""

import operator
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from rich.cells import cell_len

if TYPE_CHECKING:
    from collections.abc import Callable

#: Name of the synthetic root. Every absolute path hangs off it, and it is never walked
#: -- callers that want a total line prepend their own, and can read this name off the
#: root node they were handed.
ROOT_NAME = "/"

#: Name of the synthetic child holding a node's own row (see the module docstring).
SELF_ROW_NAME = "."

#: Node attributes describing the node itself rather than the subtree under it, and so the
#: ones ``_copy_subtree_state`` must leave alone.
_OWN_SLOTS = frozenset({"name", "children", "row"})


class PathTreeNode[T]:
    """
    Structural (``row is None``) or a leaf carrying one caller row. Both can have children,
    though ``build_path_tree`` splits a row-carrying node that does.
    """

    __slots__ = ("children", "name", "row", "subtree_row_count")

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, PathTreeNode[T]] = {}
        self.row: T | None = None
        #: Rows at this node and every descendant; filled by ``build_path_tree``.
        self.subtree_row_count = 0


class PathTreeLine[T](NamedTuple):
    #: Tree prefix plus the node's name, with a trailing "/" when it has children.
    label: str
    #: Length of the glyph prefix alone, so a renderer can leave it unstyled.
    prefix_len: int
    #: The node this line stands for -- ``node.row`` is None on a structural line.
    node: PathTreeNode[T]


def build_path_tree[T](
    rows: list[tuple[str, T]],
    *,
    node_factory: Callable[[str], PathTreeNode[T]] = PathTreeNode,
) -> PathTreeNode[T]:
    """
    Build the trie over ``(path, row)`` pairs and normalize it for display.

    Every node, synthetic ones included, comes from *node_factory*. A path with no segments
    is skipped rather than attached to the root, which would put an unnamed line at the top.
    """
    root = node_factory(ROOT_NAME)
    for path, row in rows:
        parts = Path(path).parts
        if not parts:
            continue
        # On absolute paths Path.parts starts with "/"; the synthetic root stands for it.
        segments = parts[1:] if parts[0] == ROOT_NAME else parts
        cur = root
        for seg in segments:
            if seg not in cur.children:
                cur.children[seg] = node_factory(seg)
            cur = cur.children[seg]
        cur.row = row

    _compress(root)
    _split_self_rows(root, node_factory)
    _count_rows(root)
    return root


def _compress[T](node: PathTreeNode[T]) -> None:
    """
    Fold ``parent/child`` into one node when the parent is structural and has exactly
    one child. The synthetic root is exempt (it stays as ``/``).
    """
    new_children: dict[str, PathTreeNode[T]] = {}
    for child in list(node.children.values()):
        _compress(child)
        while child.row is None and len(child.children) == 1:
            (gc,) = child.children.values()
            gc.name = f"{child.name}/{gc.name}"
            child = gc  # noqa: PLW2901
        new_children[child.name] = child
    node.children = new_children


def _split_self_rows[T](
    node: PathTreeNode[T], node_factory: Callable[[str], PathTreeNode[T]]
) -> None:
    """
    For row-carrying nodes that also have children (e.g. ``mm-builder`` with
    ``mm-builder/mm_random`` underneath), move the node's own row to a synthetic ``.``
    child so the parent can render as a structural heading.
    """
    for child in list(node.children.values()):
        _split_self_rows(child, node_factory)
    if node.row is not None and node.children:
        dot = node_factory(SELF_ROW_NAME)
        dot.row = node.row
        node.row = None
        new_children: dict[str, PathTreeNode[T]] = {SELF_ROW_NAME: dot}
        new_children.update(node.children)
        node.children = new_children


def _count_rows[T](node: PathTreeNode[T]) -> None:
    """Post-order: fill ``subtree_row_count`` on every node."""
    node.subtree_row_count = 1 if node.row is not None else 0
    for child in node.children.values():
        _count_rows(child)
        node.subtree_row_count += child.subtree_row_count


def walk_path_tree[T](root: PathTreeNode[T]) -> list[PathTreeLine[T]]:
    """
    Walk the tree in display order, yielding one line per visible node.

    The root itself is not emitted; a caller that wants a total line prepends its own.
    """
    out: list[PathTreeLine[T]] = []

    def walk(node: PathTreeNode[T], ancestors_continue: list[bool]) -> None:
        children = list(node.children.values())
        # `.` is pinned first (it represents the parent directory's own row, so it
        # visually belongs immediately under the parent). Then leaves (no children of
        # their own) alphabetically, then subtree nodes ordered by ascending row count
        # so the bushiest groups sink to the bottom.
        dot = [c for c in children if c.name == SELF_ROW_NAME]
        leaves = sorted(
            (c for c in children if c.name != SELF_ROW_NAME and not c.children),
            key=operator.attrgetter("name"),
        )
        nodes = sorted(
            (c for c in children if c.name != SELF_ROW_NAME and c.children),
            key=operator.attrgetter("subtree_row_count", "name"),
        )
        ordered = dot + leaves + nodes

        for i, child in enumerate(ordered):
            is_last = i == len(ordered) - 1
            connector = "└" if is_last else "├"
            prefix = "".join("│" if cont else " " for cont in ancestors_continue) + connector
            label = prefix + child.name
            if child.children:
                label += "/"
            out.append(PathTreeLine(label=label, prefix_len=len(prefix), node=child))

            if child.children:
                walk(child, [*ancestors_continue, not is_last])

    walk(root, [])
    return out


def expand_widest_chain[T](root: PathTreeNode[T]) -> bool:
    """
    Give one segment back on the widest folded line's whole sibling group -- the inverse of
    `_compress`, driven in a render/measure/call-again loop until the output fits.

    A whole sibling group is split at once: splitting turns a sibling into a subtree node
    and `walk_path_tree` sorts those after the leaves, so chopping one line of a group would
    reorder the group around it. A split is not always a win either -- everything under the
    folded line drops a level and gains a glyph character, so it can *widen* the tree when a
    deep leaf was already setting the width; the group is measured as a whole and a widening
    split is undone and reported as nothing left to do.

    Ties are allowed through, since two equally wide groups must be split one at a time.
    Termination holds because every ``True`` removes at least one ``/``.
    """
    before = _max_label_width(root, 1)
    found = _widest_folded(root, 1, None)
    if found is None:
        return False
    _, parent, _node = found
    # A snapshot, because each split rebuilds `parent.children`; it only ever substitutes
    # the split child's own key, so the siblings still to come are untouched.
    group = [child for child in parent.children.values() if "/" in child.name]
    stems = [(_split_first_segment(parent, child), child) for child in group]
    if _max_label_width(root, 1) > before:
        # Last in, first out: each undo restores one key in place, so unwinding in reverse
        # returns the dict to exactly the order it had.
        for stem, child in reversed(stems):
            _unsplit(parent, stem, child)
        return False
    return True


def _max_label_width[T](node: PathTreeNode[T], depth: int) -> int:
    widest = 0
    for child in node.children.values():
        width = depth + cell_len(child.name) + (1 if child.children else 0)
        widest = max(widest, width, _max_label_width(child, depth + 1))
    return widest


def _widest_folded[T](
    node: PathTreeNode[T],
    depth: int,
    best: tuple[int, PathTreeNode[T], PathTreeNode[T]] | None,
) -> tuple[int, PathTreeNode[T], PathTreeNode[T]] | None:
    """
    Width is derived from *depth* rather than measured off a walk: a glyph prefix is exactly
    one character per level.
    """
    for child in node.children.values():
        if "/" in child.name:
            width = depth + cell_len(child.name) + (1 if child.children else 0)
            if best is None or width > best[0]:
                best = (width, node, child)
        best = _widest_folded(child, depth + 1, best)
    return best


def _split_first_segment[T](parent: PathTreeNode[T], node: PathTreeNode[T]) -> PathTreeNode[T]:
    """
    Move *node*'s leading segment into a new structural parent, and return that parent.

    *parent*'s dict is rebuilt rather than mutated: the new node has to take the old key's
    *position*, so the insertion order ``walk_path_tree`` reads is the one the tree was built
    with. Built from ``type(node)``, so a caller's subclass cannot drift.
    """
    old_key = node.name
    head, _, tail = old_key.partition("/")
    stem = type(node)(head)
    _copy_subtree_state(stem, node)
    node.name = tail
    stem.children = {tail: node}
    parent.children = {
        (head if key == old_key else key): (stem if key == old_key else child)
        for key, child in parent.children.items()
    }
    return stem


def _unsplit[T](parent: PathTreeNode[T], stem: PathTreeNode[T], node: PathTreeNode[T]) -> None:
    """
    Put back what `_split_first_segment` took apart. The stem held nothing of its own -- no
    row, only aggregates copied from *node* -- so dropping it restores the tree exactly, down
    to *node*'s place among its siblings.
    """
    node.name = f"{stem.name}/{node.name}"
    parent.children = {
        (node.name if key == stem.name else key): (node if key == stem.name else child)
        for key, child in parent.children.items()
    }


def _copy_subtree_state[T](dst: PathTreeNode[T], src: PathTreeNode[T]) -> None:
    """
    Copy every subtree aggregate from *src* onto *dst*, leaving *dst*'s own identity alone.

    A split stem stands for exactly the subtree it took its segment from, so aggregates
    transfer verbatim rather than being recomputed -- which is what lets a caller re-walk
    an expanded tree without re-running an aggregation pass that is not idempotent.

    Slot-driven, so a subclass's extra aggregates come along. ``hasattr`` guards the read
    because a subclass may declare a slot it fills later --
    ``agent_wrap.cli.stats.tree.Node.subtree_bucket`` is annotated at class level and
    assigned by a separate pass.
    """
    for klass in type(src).__mro__:
        for slot in getattr(klass, "__slots__", ()):
            if slot not in _OWN_SLOTS and hasattr(src, slot):
                setattr(dst, slot, getattr(src, slot))
