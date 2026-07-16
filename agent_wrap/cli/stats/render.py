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

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from agent_wrap.cli.stats.tree import DisplayRow, Node, build_project_tree, flatten_tree
from agent_wrap.domain.display.models import Ansi, RowItem, RowItemOrDivider
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.domain.stats.constants import ORPHANED_LABEL

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.stats.models import OrphanedResult, ProjectRow

# A "cost view" callback. cost/unknown MUST come from this, never `Bucket.cost`.
CostFn = Callable[[str, Bucket], tuple[float, bool]]
BuildModelSection = Callable[[dict[str, Bucket], int, "DisplayService"], list[RowItemOrDivider]]


class AggregatedDayRows(NamedTuple):
    """Aggregated per-day rows with totals for the By-day table."""

    day_rows_data: list[tuple[str, Bucket, float, bool]]
    total_b: Bucket
    total_cost: float
    total_unknown: bool


_DIV = "__div__"


def range_label(from_iso: str | None, until_iso: str | None) -> str:
    """Human-readable label for an inclusive range, for table titles."""
    if from_iso is None and until_iso is None:
        return "all time"
    if from_iso is None:
        return f"through {until_iso}"
    if until_iso is None:
        return f"{from_iso} onward"
    if from_iso == until_iso:
        return from_iso
    return f"{from_iso} … {until_iso}"


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
                display.format_count(tree_root.subtree_bucket.msgs),
                display.format_count(tree_root.subtree_bucket.in_),
                display.format_count(tree_root.subtree_bucket.out),
                display.format_count(tree_root.subtree_bucket.cw),
                display.format_count(tree_root.subtree_bucket.cr),
                display.format_cost_with_unknown(
                    tree_root.subtree_known_cost, unknown=tree_root.subtree_unknown
                ),
            ],
            style=Ansi.DIM,
            prefix_len=0,
        )
    )
    for dr in display_rows:
        if dr.transient:
            style = Ansi.CYAN
        elif dr.is_structural:
            style = Ansi.DIM
        else:
            style = Ansi.NONE
        body.append(
            RowItem(
                cells=[
                    dr.label,
                    str(dr.sessions),
                    display.format_timestamp(dr.last_ts),
                    display.format_count(dr.bucket.msgs),
                    display.format_count(dr.bucket.in_),
                    display.format_count(dr.bucket.out),
                    display.format_count(dr.bucket.cw),
                    display.format_count(dr.bucket.cr),
                    dr.cost_str,
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
                    display.format_count(b.msgs),
                    display.format_count(b.in_),
                    display.format_count(b.out),
                    display.format_count(b.cw),
                    display.format_count(b.cr),
                    display.format_cost_with_unknown(b.cost, unknown=b.cost_unknown),
                ],
                style=Ansi.CYAN,
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
        day_total = Bucket()
        day_cost: float = 0.0
        day_unknown = False
        for model, b in dated[d].items():
            day_total.merge(b)
            known, unknown = cost_fn(model, b)
            if unknown:
                day_unknown = True
            else:
                day_cost += known
        day_rows_data.append((d, day_total, day_cost, day_unknown))

    total_b = Bucket()
    total_cost: float = 0.0
    total_unknown = False
    for _, b, c, unk in day_rows_data:
        total_b.merge(b)
        if unk:
            total_unknown = True
        else:
            total_cost += c

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
            body.append(_DIV)

        day_rows_data, total_b, total_cost, total_unknown = _aggregate_day_rows(
            dated, shown_days, cost_fn
        )

        for d, b, day_cost, day_unknown in reversed(day_rows_data):
            cost_str = display.format_cost_with_unknown(day_cost, unknown=day_unknown)
            body.append(
                RowItem(
                    cells=[
                        d,
                        display.format_count(b.msgs),
                        display.format_count(b.in_),
                        display.format_count(b.out),
                        display.format_count(b.cw),
                        display.format_count(b.cr),
                        cost_str,
                    ],
                    style=Ansi.NONE,
                    prefix_len=0,
                )
            )

        body.append(_DIV)
        body.append(
            RowItem(
                cells=[
                    "TOTAL",
                    display.format_count(total_b.msgs),
                    display.format_count(total_b.in_),
                    display.format_count(total_b.out),
                    display.format_count(total_b.cw),
                    display.format_count(total_b.cr),
                    display.format_cost_with_unknown(total_cost, unknown=total_unknown),
                ],
                style=Ansi.BOLD_YELLOW,
                prefix_len=0,
            )
        )
        # Average over the days that actually have activity in the window.
        n_days = len(shown_days)
        body.append(
            RowItem(
                cells=[
                    "DAILY AVG",
                    display.format_count(total_b.msgs // n_days),
                    display.format_count(total_b.in_ // n_days),
                    display.format_count(total_b.out // n_days),
                    display.format_count(total_b.cw // n_days),
                    display.format_count(total_b.cr // n_days),
                    display.format_cost_with_unknown(total_cost / n_days, unknown=total_unknown),
                ],
                style=Ansi.BOLD_YELLOW,
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
    # by a `├─┼─┤` divider; widths of the trailing six numeric columns are shared
    # across both tables so the numbers line up vertically.
    shared_headers = ["MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    shared_aligns = [">", ">", ">", ">", ">", ">"]
    n_shared = len(shared_headers)
    label = range_label(from_iso, until_iso)

    # === Projects table: per-project tree ===
    total_headers = ["PROJECT", "SESSIONS", "LAST LAUNCH", *shared_headers]
    total_aligns = ["<", ">", "<", *shared_aligns]

    tree_root = build_project_tree(rows)
    display_rows = flatten_tree(tree_root, display=display)

    total_body = _build_total_body(tree_root, display_rows, display, orphaned)

    # === By-day table: models (in window) + per-day (in window) + TOTAL ===
    recent_headers = ["MODEL / DATE", *shared_headers]
    recent_aligns = ["<", *shared_aligns]

    recent_body = _build_recent_body(totals_by_day_by_model, cost_fn, build_model_section, display)

    # === Shared widths for the trailing six numeric columns ===
    shared_widths = display.compute_shared_widths(
        [(total_headers, total_body, 3), (recent_headers, recent_body, 1)],
        n_shared,
    )

    lines: list[str] = []
    lines.extend(
        display.render_table(
            f"Projects ({label}):",
            total_headers,
            total_aligns,
            total_body,
            3,
            shared_widths,
        )
    )
    if recent_body:
        lines.append("")
        lines.extend(
            display.render_table(
                f"By day ({label}):",
                recent_headers,
                recent_aligns,
                recent_body,
                1,
                shared_widths,
            )
        )

    return "\n".join(lines)
