# This file has been edited with the assistance of an AI tool.
"""
The stats-specific half of the per-project (and per-model) tree.

The trie itself -- construction, compression, the synthetic ``.`` self-rows, sibling
order and the ``├``/``└`` glyphs -- lives in :mod:`agent_wrap.lib.path_tree`, which knows
nothing about sessions or cost. What is left here is what those glyphs are decorated
with: the subtree totals a structural line reports, and the mapping from one walked line
to one display row.
"""

from typing import TYPE_CHECKING, cast

from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.lib.path_tree import PathTreeNode, build_path_tree, walk_path_tree

if TYPE_CHECKING:
    from datetime import datetime

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.stats.models import ProjectRow


class Node(PathTreeNode["ProjectRow"]):
    """
    A trie node carrying the totals a structural line reports.

    ``subtree_*`` fields are aggregates over the node and all its descendants, populated
    by `_aggregate` after `build_path_tree` has compressed the trie and split self-rows.
    They sit on the node rather than in a table beside it because that is where both
    `flatten_tree` and the caller rendering the root's own line read them.
    """

    __slots__ = (
        "subtree_bucket",
        "subtree_known_cost",
        "subtree_last_ts",
        "subtree_sessions",
        "subtree_unknown",
    )

    def __init__(self, name: str) -> None:
        super().__init__(name)
        #: Sum over this node and all descendants; assigned by ``_aggregate``.
        self.subtree_bucket: Bucket
        self.subtree_known_cost = 0.0
        self.subtree_unknown = False
        self.subtree_sessions = 0
        self.subtree_last_ts: datetime | None = None


class DisplayRow:
    __slots__ = (
        "bucket",
        "cost_str",
        "is_structural",
        "label",
        "last_ts",
        "prefix_len",
        "sessions",
        "transient",
    )

    def __init__(  # noqa: PLR0913
        self,
        label: str,
        prefix_len: int,
        *,
        is_structural: bool,
        sessions: int,
        bucket: Bucket,
        last_ts: datetime | None,
        cost_str: str,
        transient: bool = False,
    ) -> None:
        self.label = label
        self.prefix_len = prefix_len
        self.is_structural = is_structural
        self.sessions = sessions
        self.bucket = bucket
        self.last_ts = last_ts
        self.cost_str = cost_str
        self.transient = transient


def build_project_tree(rows: list[ProjectRow]) -> Node:
    """Build the display-ready trie over `rows`, then fill in the subtree totals."""
    root = cast("Node", build_path_tree([(str(r["path"]), r) for r in rows], node_factory=Node))
    _aggregate(root)
    return root


def _aggregate(node: Node) -> None:
    """Post-order: fill `subtree_*` fields on every node."""
    contributions: list[Bucket] = []
    for raw in node.children.values():
        child = cast("Node", raw)
        _aggregate(child)
        contributions.append(child.subtree_bucket)
        node.subtree_known_cost += child.subtree_known_cost
        if child.subtree_unknown:
            node.subtree_unknown = True
        node.subtree_sessions += child.subtree_sessions
        if child.subtree_last_ts is not None and (
            node.subtree_last_ts is None or child.subtree_last_ts > node.subtree_last_ts
        ):
            node.subtree_last_ts = child.subtree_last_ts
    if node.row is not None:
        r = node.row
        contributions.append(r["total"])
        if r["cost"] is None:
            node.subtree_unknown = True
        else:
            node.subtree_known_cost += r["cost"]
        node.subtree_sessions += r["sessions"]
        if r["last_ts"] is not None and (
            node.subtree_last_ts is None or r["last_ts"] > node.subtree_last_ts
        ):
            node.subtree_last_ts = r["last_ts"]
    node.subtree_bucket = Bucket.merged(contributions)


def flatten_tree(root: Node, *, display: DisplayService) -> list[DisplayRow]:
    """
    Turn every walked line into a display row. The root itself is not emitted; callers
    prepend their own banner.
    """
    out: list[DisplayRow] = []
    for line in walk_path_tree(root):
        node = cast("Node", line.node)
        # A grouped transient project's (`.agent_stats_leaf`) row is accented in color by
        # the renderer via the DisplayRow.transient flag; the row's own path-segment name
        # already equals its group's display name (both derive from the marker directory).
        transient = bool(node.row is not None and node.row.get("transient"))
        label = line.label
        if node.row is not None and not node.row["exists"]:
            label += " (missing)"

        if node.row is not None:
            r = node.row
            out.append(
                DisplayRow(
                    label=label,
                    prefix_len=line.prefix_len,
                    is_structural=False,
                    sessions=r["sessions"],
                    bucket=r["total"],
                    last_ts=r["last_ts"],
                    cost_str=display.format_cost(r["cost"]),
                    transient=transient,
                )
            )
        else:
            out.append(
                DisplayRow(
                    label=label,
                    prefix_len=line.prefix_len,
                    is_structural=True,
                    sessions=node.subtree_sessions,
                    bucket=node.subtree_bucket,
                    last_ts=node.subtree_last_ts,
                    cost_str=display.format_cost_with_unknown(
                        node.subtree_known_cost, unknown=node.subtree_unknown
                    ),
                )
            )
    return out
