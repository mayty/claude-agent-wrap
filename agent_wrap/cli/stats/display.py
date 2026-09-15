# This file has been edited with the assistance of an AI tool.
"""
Terminal rendering for the stats command.

`stats` emits two stacked tables over the same usage window — "Projects"
(per-project tree) and "By day" (per-model + per-day) — with the trailing six
numeric columns width-aligned across both. The windowing is applied at scan
time (the per-day dict and the project rows are already restricted to the
range), so this layer just renders what it is given.
"""

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Group

from agent_wrap.cli.stats.constants import PROJECTS_TABLE, RECENT_TABLE
from agent_wrap.cli.stats.tree import DisplayRow, Node, build_project_tree, flatten_tree
from agent_wrap.constants import DIVIDER, ORPHANED_LABEL
from agent_wrap.domain.display.constants import Style
from agent_wrap.domain.display.models import RowItem, RowItemOrDivider
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.lib.path_tree import expand_widest_chain

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.stats.models import OrphanedResult, ProjectRow


def range_label(from_iso: str | None, until_iso: str | None) -> str:
    if from_iso is None and until_iso is None:
        return "all time"
    if from_iso is None:
        return f"through {until_iso}"
    if until_iso is None:
        return f"{from_iso} onward"
    if from_iso == until_iso:
        return from_iso
    return f"{from_iso} … {until_iso}"


def usage_cells(bucket: Bucket, *, cost: str, display: DisplayService, scale: int = 1) -> list[str]:
    """
    Render *bucket* as the trailing columns of a usage table, in `USAGE_HEADERS` order.

    *cost* is already formatted, because the callers reach it three different ways --
    from ``Bucket.cost`` paired with ``cost_unknown``, from a subtree's known/unknown
    pair, or from a `DisplayRow`'s precomputed string. ``Bucket.cost`` alone is never
    enough: that float cannot tell "known to be zero" from "unknown", which is the
    ``$0.00`` / ``?`` / ``$X+?`` distinction, and ``cost_unknown`` is what carries it.

    *scale* divides every count, for the DAILY AVG row. It is a parameter rather than a
    pre-divided `Bucket` because bucket construction stays inside the pricing domain.
    """
    return [
        display.format_count(bucket.msgs // scale),
        display.format_count(bucket.in_ // scale),
        display.format_count(bucket.out // scale),
        display.format_count(bucket.cw // scale),
        display.format_count(bucket.cr // scale),
        cost,
    ]


def _model_display_rows(
    totals_by_model: dict[str, Bucket], display: DisplayService
) -> list[DisplayRow]:
    """
    Render the per-model breakdown as a provider/model tree.

    Reuses the same trie machinery as the project tree by treating each
    `provider/model` key as a path that `Path(...).parts` splits on `/`.
    Models carry no session/launch data, so those columns are left blank by
    the callers; only the token/cost columns of each DisplayRow are used.
    """
    rows: list[ProjectRow] = [
        {
            "path": Path(model),
            "exists": True,
            "sessions": 0,
            "last_ts": None,
            "total": bucket,
            "cost": None if bucket.cost_unknown else bucket.cost,
        }
        for model, bucket in totals_by_model.items()
    ]
    return flatten_tree(build_project_tree(rows), display=display)


def _build_model_section(
    totals_by_model: dict[str, Bucket],
    leading_blanks: int,
    display: DisplayService,
) -> list[RowItemOrDivider]:
    """
    Build the per-model breakdown body rows (provider/model tree).

    `leading_blanks` empty cells follow the label to skip the SESSIONS /
    LAST LAUNCH columns in the Total table (2) versus the Recent table (0).
    """
    blanks = [""] * leading_blanks
    body: list[RowItemOrDivider] = []
    for dr in _model_display_rows(totals_by_model, display):
        style = Style.DIM if dr.is_structural else Style.NONE
        body.append(
            RowItem(
                cells=[
                    dr.label,
                    *blanks,
                    *usage_cells(dr.bucket, cost=dr.cost_str, display=display),
                ],
                style=style,
                prefix_len=dr.prefix_len,
            )
        )
    return body


def _build_total_body(
    tree_root: Node,
    display_rows: list[DisplayRow],
    display: DisplayService,
    orphaned: OrphanedResult | None = None,
) -> list[RowItemOrDivider]:
    body: list[RowItemOrDivider] = []

    body.append(
        RowItem(
            cells=[
                "/",
                str(tree_root.subtree_sessions),
                display.format_timestamp(tree_root.subtree_last_ts),
                *usage_cells(
                    tree_root.subtree_bucket,
                    cost=display.format_cost_with_unknown(
                        tree_root.subtree_known_cost, unknown=tree_root.subtree_unknown
                    ),
                    display=display,
                ),
            ],
            style=Style.DIM,
            prefix_len=0,
        )
    )
    for dr in display_rows:
        if dr.transient:
            style = Style.CYAN
        elif dr.is_structural:
            style = Style.DIM
        else:
            style = Style.NONE
        body.append(
            RowItem(
                cells=[
                    dr.label,
                    str(dr.sessions),
                    display.format_timestamp(dr.last_ts),
                    *usage_cells(dr.bucket, cost=dr.cost_str, display=display),
                ],
                style=style,
                prefix_len=dr.prefix_len,
            )
        )

    if orphaned is not None:
        # A sibling of the "/" root (prefix_len 0, not under the fs tree): logs
        # left behind by deleted projects. Its usage is already folded into the
        # per-model section of the By-day table.
        b = orphaned["total"]
        body.append(
            RowItem(
                cells=[
                    ORPHANED_LABEL,
                    str(orphaned["sessions"]),
                    display.format_timestamp(orphaned["last_ts"]),
                    *usage_cells(
                        b,
                        cost=display.format_cost_with_unknown(b.cost, unknown=b.cost_unknown),
                        display=display,
                    ),
                ],
                style=Style.CYAN,
                prefix_len=0,
            )
        )

    return body


def _build_recent_body(
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    display: DisplayService,
) -> list[RowItemOrDivider]:
    body: list[RowItemOrDivider] = []

    # The day dict is already restricted to the window at scan time; the
    # synthetic "?" key (records with no timestamp) is the one exception and is
    # only present in the all-time view, where it is shown alongside dated days.
    dated = {d: m for d, m in totals_by_day_by_model.items() if d != "?"}
    shown_days = sorted(dated.keys(), reverse=True)

    recent_models: dict[str, Bucket] = defaultdict(Bucket)
    for d in shown_days:
        for model, b in dated[d].items():
            recent_models[model].merge(b)

    if recent_models:
        body.extend(_build_model_section(dict(recent_models), 0, display))

    if not shown_days:
        return body

    if body:
        body.append(DIVIDER)

    # A day carrying even one unpriced model contributes nothing to its own total and
    # nothing to the grand total, rather than a known-good partial sum that would read
    # as the whole day's spend. The "+?" the formatter appends is the rest of it.
    day_rows = [
        (
            d,
            Bucket.merged(dated[d].values()),
            sum(b.cost for b in dated[d].values() if not b.cost_unknown),
            any(b.cost_unknown for b in dated[d].values()),
        )
        for d in shown_days
    ]
    total_cost = sum(cost for _d, _b, cost, unknown in day_rows if not unknown)
    total_unknown = any(unknown for _d, _b, _c, unknown in day_rows)
    total_b = Bucket.merged(b for _d, b, _c, _unk in day_rows)

    for d, b, day_cost, day_unknown in reversed(day_rows):
        body.append(
            RowItem(
                cells=[
                    d,
                    *usage_cells(
                        b,
                        cost=display.format_cost_with_unknown(day_cost, unknown=day_unknown),
                        display=display,
                    ),
                ],
                style=Style.NONE,
                prefix_len=0,
            )
        )

    body.append(DIVIDER)
    body.append(
        RowItem(
            cells=[
                "TOTAL",
                *usage_cells(
                    total_b,
                    cost=display.format_cost_with_unknown(total_cost, unknown=total_unknown),
                    display=display,
                ),
            ],
            style=Style.BOLD_YELLOW,
            prefix_len=0,
        )
    )
    # Average over the days that actually have activity in the window.
    n_days = len(shown_days)
    body.append(
        RowItem(
            cells=[
                "DAILY AVG",
                *usage_cells(
                    total_b,
                    cost=display.format_cost_with_unknown(
                        total_cost / n_days, unknown=total_unknown
                    ),
                    display=display,
                    scale=n_days,
                ),
            ],
            style=Style.BOLD_YELLOW,
            prefix_len=0,
        )
    )

    return body


def render(  # noqa: PLR0913
    rows: list[ProjectRow],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    from_iso: str | None,
    until_iso: str | None,
    *,
    orphaned: OrphanedResult | None = None,
    display: DisplayService,
) -> Group:
    # Two stacked tables over the same window: "Projects" (per-project tree) and
    # "By day" (per-model + per-day). Each table has internal sections separated
    # by a `├─┼─┤` divider; the trailing numeric columns are width-aligned across
    # both, which is what `fit_table`'s `others` carries.
    label = range_label(from_iso, until_iso)
    tree_root = build_project_tree(rows)

    # Built before the Projects body because it does not depend on the project tree, and
    # the fit loop below rebuilds that body several times.
    recent_body = _build_recent_body(totals_by_day_by_model, display)

    # Chop the tree down until the table fits the console: `_compress` folds a chain nothing
    # branches on into one very wide node, which is exactly the shape that overflows, and
    # splitting it back out spends a line of height to buy a segment of width.
    total_body, shared_widths = display.fit_table(
        PROJECTS_TABLE,
        lambda: _build_total_body(
            tree_root, flatten_tree(tree_root, display=display), display, orphaned
        ),
        shrink=lambda: expand_widest_chain(tree_root),
        others=[(RECENT_TABLE, recent_body)],
    )

    projects = display.render_table(
        f"Projects ({label}):", PROJECTS_TABLE, total_body, shared_widths
    )
    if not recent_body:
        return Group(projects)
    return Group(
        projects,
        "",
        display.render_table(f"By day ({label}):", RECENT_TABLE, recent_body, shared_widths),
    )
