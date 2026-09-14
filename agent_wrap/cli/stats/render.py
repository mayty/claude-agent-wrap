# This file has been created with the assistance of an AI tool.
"""
Shared render core for the usage-stats command.

`stats` emits two stacked tables over the same usage window — "Projects"
(per-project tree) and "By day" (per-model + per-day) — with the trailing six
numeric columns width-aligned across both. The windowing is applied at scan
time (the per-day dict and the project rows are already restricted to the
range), so this layer just renders what it is given.

Two things are injected by the caller:

  * `cost_fn(model, bucket) -> (known_cost, unknown)` — how a (model, bucket)
    pair's cost is obtained. `stats` reads the cost baked into `Bucket.cost`
    at scan time. IMPORTANT: cost/unknown for the per-day rows MUST come from
    `cost_fn`, never by reading `Bucket.cost` directly — that float cannot
    represent the "known-but-zero" vs "unknown" (`?` / `$X+?`) distinction.

  * `build_model_section(totals_by_model, leading_blanks) -> list[body_row]` —
    the per-model breakdown rendered as a provider/model tree. `leading_blanks`
    is the number of empty columns to insert after the label (0 in the By-day
    table, which has no SESSIONS/LAST LAUNCH columns).
"""

from collections import defaultdict
from typing import TYPE_CHECKING

from agent_wrap.cli.stats.constants import PROJECTS_TABLE, RECENT_TABLE
from agent_wrap.cli.stats.models import AggregatedDayRows, BuildModelSection, CostFn
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

    *cost* is already formatted, because the callers reach it four different ways -- from
    ``Bucket.cost``, from a subtree's known/unknown pair, from ``cost_fn``, or from a
    `DisplayRow`'s precomputed string -- and the per-day rows must not read ``Bucket.cost``
    directly (see this module's docstring).

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


def _aggregate_day_rows(
    dated: dict[str, dict[str, Bucket]],
    shown_days: list[str],
    cost_fn: CostFn,
) -> AggregatedDayRows:
    day_rows_data: list[tuple[str, Bucket, float, bool]] = []
    for d in shown_days:
        day_cost: float = 0.0
        day_unknown = False
        for model, b in dated[d].items():
            known, unknown = cost_fn(model, b)
            if unknown:
                day_unknown = True
            else:
                day_cost += known
        day_rows_data.append((d, Bucket.merged(dated[d].values()), day_cost, day_unknown))

    total_cost = sum(c for _d, _b, c, unk in day_rows_data if not unk)
    total_unknown = any(unk for _d, _b, _c, unk in day_rows_data)
    total_b = Bucket.merged(b for _d, b, _c, _unk in day_rows_data)

    return AggregatedDayRows(day_rows_data, total_b, total_cost, total_unknown)


def _build_recent_body(
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    cost_fn: CostFn,
    build_model_section: BuildModelSection,
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
        body.extend(build_model_section(dict(recent_models), 0, display))

    if shown_days:
        if body:
            body.append(DIVIDER)

        day_rows_data, total_b, total_cost, total_unknown = _aggregate_day_rows(
            dated, shown_days, cost_fn
        )

        for d, b, day_cost, day_unknown in reversed(day_rows_data):
            cost_str = display.format_cost_with_unknown(day_cost, unknown=day_unknown)
            body.append(
                RowItem(
                    cells=[d, *usage_cells(b, cost=cost_str, display=display)],
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


def render_core(  # noqa: PLR0913
    rows: list[ProjectRow],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    from_iso: str | None,
    until_iso: str | None,
    *,
    cost_fn: CostFn,
    build_model_section: BuildModelSection,
    orphaned: OrphanedResult | None = None,
    display: DisplayService,
) -> str:
    # Two stacked tables over the same window: "Projects" (per-project tree) and
    # "By day" (per-model + per-day). Each table has internal sections separated
    # by a `├─┼─┤` divider; the trailing numeric columns are width-aligned across
    # both, which is what `fit_table`'s `others` carries.
    label = range_label(from_iso, until_iso)
    tree_root = build_project_tree(rows)

    # Built before the Projects body because it does not depend on the project tree, and
    # the fit loop below rebuilds that body several times.
    recent_body = _build_recent_body(totals_by_day_by_model, cost_fn, build_model_section, display)

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

    lines: list[str] = list(
        display.render_table(f"Projects ({label}):", PROJECTS_TABLE, total_body, shared_widths)
    )
    if recent_body:
        lines.append("")
        lines.extend(
            display.render_table(f"By day ({label}):", RECENT_TABLE, recent_body, shared_widths)
        )

    return "\n".join(lines)
