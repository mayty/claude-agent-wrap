# This file has been edited with the assistance of an AI tool.
"""Path-trie machinery for rendering the per-project (and per-model) tree."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.domain.pricing.models import Bucket

if TYPE_CHECKING:
    from datetime import datetime

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.stats.models import ProjectRow


class Node:
    """
    One node in the path trie used to render the per-project tree.

    A node is either *structural* (`row is None`, e.g. `/`, `home/`, an
    intermediate path segment) or a *project* node carrying the row dict
    produced by `scan_project`. `subtree_*` fields are aggregates over the
    node and all its descendants, populated by `_aggregate` after the trie
    has been compressed and self-rows split.
    """

    __slots__ = (
        "children",
        "name",
        "row",
        "subtree_bucket",
        "subtree_known_cost",
        "subtree_last_ts",
        "subtree_project_count",
        "subtree_sessions",
        "subtree_unknown",
    )

    def __init__(self, name: str) -> None:
        self.name = name
        self.children: dict[str, Node] = {}
        self.row: ProjectRow | None = None
        self.subtree_bucket = Bucket()
        self.subtree_known_cost = 0.0
        self.subtree_unknown = False
        self.subtree_sessions = 0
        self.subtree_last_ts: datetime | None = None
        self.subtree_project_count = 0


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
    """
    Build a path trie over `rows`, then compress single-child structural
    chains and split projects-with-children into a `.` self-row.
    """
    root = Node("/")
    for r in rows:
        parts = Path(r["path"]).parts
        if not parts:
            continue
        # On absolute paths Path.parts starts with "/"; we use the synthetic
        # root for that. Relative paths (shouldn't appear in projects.txt
        # but handled defensively) are placed under root as well.
        segments = parts[1:] if parts[0] == "/" else parts
        cur = root
        for seg in segments:
            if seg not in cur.children:
                cur.children[seg] = Node(seg)
            cur = cur.children[seg]
        cur.row = r

    _compress(root)
    _split_self_rows(root)
    _aggregate(root)
    return root


def _compress(node: Node) -> None:
    """
    Fold `parent/child` into one node when the parent is structural and
    has exactly one child. The synthetic root is exempt (it stays as `/`).
    """
    new_children: dict[str, Node] = {}
    for child in list(node.children.values()):
        _compress(child)
        while child.row is None and len(child.children) == 1:
            (gc,) = child.children.values()
            gc.name = f"{child.name}/{gc.name}"
            child = gc  # noqa: PLW2901
        new_children[child.name] = child
    node.children = new_children


def _split_self_rows(node: Node) -> None:
    """
    For project nodes that also have children (e.g. `mm-builder` with
    `mm-builder/mm_random` underneath), move the project's own row to a
    synthetic `.` child so the parent can render as a structural subtotal.
    """
    for child in list(node.children.values()):
        _split_self_rows(child)
    if node.row is not None and node.children:
        dot = Node(".")
        dot.row = node.row
        node.row = None
        new_children: dict[str, Node] = {".": dot}
        new_children.update(node.children)
        node.children = new_children


def _aggregate(node: Node) -> None:
    """Post-order: fill `subtree_*` fields on every node."""
    for child in node.children.values():
        _aggregate(child)
        node.subtree_bucket.merge(child.subtree_bucket)
        node.subtree_known_cost += child.subtree_known_cost
        if child.subtree_unknown:
            node.subtree_unknown = True
        node.subtree_sessions += child.subtree_sessions
        node.subtree_project_count += child.subtree_project_count
        if child.subtree_last_ts is not None and (
            node.subtree_last_ts is None or child.subtree_last_ts > node.subtree_last_ts
        ):
            node.subtree_last_ts = child.subtree_last_ts
    if node.row is not None:
        r = node.row
        node.subtree_bucket.merge(r["total"])
        if r["cost"] is None:
            node.subtree_unknown = True
        else:
            node.subtree_known_cost += r["cost"]
        node.subtree_sessions += r["sessions"]
        node.subtree_project_count += 1
        if r["last_ts"] is not None and (
            node.subtree_last_ts is None or r["last_ts"] > node.subtree_last_ts
        ):
            node.subtree_last_ts = r["last_ts"]


def flatten_tree(root: Node, *, display: DisplayService) -> list[DisplayRow]:
    """
    Walk the tree in display order, producing one DisplayRow per visible
    line. The root itself is not emitted; callers prepend their own banner.
    """
    out: list[DisplayRow] = []

    def walk(node: Node, ancestors_continue: list[bool]) -> None:
        children = list(node.children.values())
        # `.` is pinned first (it represents the parent directory's own
        # project row, so it visually belongs immediately under the parent).
        # Then leaves (no children of their own — single-project rows)
        # alphabetically, then subtree nodes ordered by ascending project
        # count so the bushiest groups sink to the bottom.
        dot = [c for c in children if c.name == "."]
        leaves = sorted(
            (c for c in children if c.name != "." and not c.children),
            key=lambda c: c.name,
        )
        nodes = sorted(
            (c for c in children if c.name != "." and c.children),
            key=lambda c: (c.subtree_project_count, c.name),
        )
        ordered = dot + leaves + nodes

        for i, child in enumerate(ordered):
            is_last = i == len(ordered) - 1
            connector = "└" if is_last else "├"
            prefix = "".join("│" if cont else " " for cont in ancestors_continue) + connector
            prefix_len = len(prefix)

            # A grouped transient project (`.agent_stats_leaf`) overrides the
            # final path segment with its group name; such rows are accented in
            # color by the renderer via the DisplayRow.transient flag. The
            # override is a no-op when the name is just the directory name
            # (empty marker), but the row is still flagged transient.
            seg = child.name
            transient = bool(child.row is not None and child.row.get("transient"))
            if child.row is not None and transient:
                head, _, _ = seg.rpartition("/")
                group_name = child.row["name"]
                seg = f"{head}/{group_name}" if head else group_name
            label = prefix + seg
            if child.children:
                label += "/"
            if child.row is not None and not child.row["exists"]:
                label += " (missing)"

            if child.row is not None:
                r = child.row
                cost_str = display.format_cost(r["cost"])
                out.append(
                    DisplayRow(
                        label=label,
                        prefix_len=prefix_len,
                        is_structural=False,
                        sessions=r["sessions"],
                        bucket=r["total"],
                        last_ts=r["last_ts"],
                        cost_str=cost_str,
                        transient=transient,
                    )
                )
            else:
                cost_str = display.format_cost_with_unknown(
                    child.subtree_known_cost, unknown=child.subtree_unknown
                )
                out.append(
                    DisplayRow(
                        label=label,
                        prefix_len=prefix_len,
                        is_structural=True,
                        sessions=child.subtree_sessions,
                        bucket=child.subtree_bucket,
                        last_ts=child.subtree_last_ts,
                        cost_str=cost_str,
                    )
                )

            if child.children:
                walk(child, [*ancestors_continue, not is_last])

    walk(root, [])
    return out
