# This file has been created with the assistance of an AI tool.
"""
Shared render core for the usage-stats subcommands.

Both `stats` and `legacy_stats` emit the same two stacked tables — "Total"
(all-time per-model + per-project tree) and "Recent" (per-model + per-day,
restricted to a days window) — with the trailing six numeric columns width-
aligned across both. Everything here is identical between the two commands
except two things, which are injected by the caller:

  * `cost_fn(model, bucket) -> (known_cost, unknown)` — how a (model, bucket)
    pair's cost is obtained. `stats` reads the cost baked into `Bucket.cost`
    at scan time; `legacy_stats` computes it lazily from a flat pricing dict.
    IMPORTANT: cost/unknown for the per-day rows MUST come from `cost_fn`,
    never by reading `Bucket.cost` directly — that float cannot represent the
    "known-but-zero" vs "unknown" (`?` / `$X+?`) distinction legacy relies on.

  * `build_model_section(totals_by_model, leading_blanks) -> list[body_row]` —
    the per-model breakdown. `stats` renders a provider/model tree;
    `legacy_stats` renders a flat cost-descending list. `leading_blanks` is the
    number of empty columns to insert after the label (2 in the Total table to
    skip SESSIONS/LAST LAUNCH, 0 in the Recent table).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from agent_wrap.lib.buckets import Bucket
from agent_wrap.lib.console import Ansi
from agent_wrap.lib.format import color, fmt_cost_with_unknown, fmt_count, fmt_ts
from agent_wrap.lib.table import compute_shared_widths, render_table
from agent_wrap.lib.tree import Node, build_project_tree, flatten_tree

# A "cost view" callback. cost/unknown MUST come from this, never `Bucket.cost`.
CostFn = Callable[[str, Bucket], tuple[float, bool]]
BuildModelSection = Callable[[dict[str, Bucket], int], list]

_DIV = "__div__"


def window_days(day_keys: Iterable[str], days_window: int) -> list[str]:
    """
    Return the day keys within the Recent window, newest first.

    The synthetic "?" key (records with no timestamp) is always excluded. When
    ``days_window`` is 0 every dated day is returned; otherwise only days on or
    after the (days_window - 1)-days-ago cutoff (host-local) survive. Shared by
    the Recent table and the verbose source breakdown so both honor the same
    window.
    """
    dated = sorted((d for d in day_keys if d != "?"), reverse=True)
    if not dated or days_window <= 0:
        return dated
    cutoff = (datetime.now().astimezone().date() - timedelta(days=days_window - 1)).isoformat()
    return [d for d in dated if d >= cutoff]


def _build_total_body(
    totals_by_model: dict[str, Bucket],
    tree_root: Node,
    display_rows: list,
    build_model_section: BuildModelSection,
    orphaned: dict | None = None,
) -> list:
    body: list = []

    if totals_by_model:
        body.extend(build_model_section(totals_by_model, 2))
        body.append(_DIV)

    body.append(
        (
            [
                "/",
                str(tree_root.subtree_sessions),
                fmt_ts(tree_root.subtree_last_ts),
                fmt_count(tree_root.subtree_bucket.msgs),
                fmt_count(tree_root.subtree_bucket.in_),
                fmt_count(tree_root.subtree_bucket.out),
                fmt_count(tree_root.subtree_bucket.cw),
                fmt_count(tree_root.subtree_bucket.cr),
                fmt_cost_with_unknown(
                    tree_root.subtree_known_cost, unknown=tree_root.subtree_unknown
                ),
            ],
            Ansi.DIM,
            0,
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
            (
                [
                    dr.label,
                    str(dr.sessions),
                    fmt_ts(dr.last_ts),
                    fmt_count(dr.bucket.msgs),
                    fmt_count(dr.bucket.in_),
                    fmt_count(dr.bucket.out),
                    fmt_count(dr.bucket.cw),
                    fmt_count(dr.bucket.cr),
                    dr.cost_str,
                ],
                style,
                dr.prefix_len,
            )
        )

    if orphaned is not None:
        # A sibling of the "/" root (prefix_len 0, not under the fs tree): logs
        # left behind by deleted projects. Its usage is already folded into the
        # per-model section above.
        b = orphaned["total"]
        body.append(
            (
                [
                    "<orphaned>",
                    str(orphaned["sessions"]),
                    fmt_ts(orphaned["last_ts"]),
                    fmt_count(b.msgs),
                    fmt_count(b.in_),
                    fmt_count(b.out),
                    fmt_count(b.cw),
                    fmt_count(b.cr),
                    fmt_cost_with_unknown(b.cost, unknown=b.cost_unknown),
                ],
                Ansi.CYAN,
                0,
            )
        )

    return body


def _aggregate_day_rows(
    dated: dict[str, dict[str, Bucket]],
    shown_days: list[str],
    cost_fn: CostFn,
) -> tuple[list[tuple[str, Bucket, float, bool]], Bucket, float, bool]:
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

    return day_rows_data, total_b, total_cost, total_unknown


def _build_recent_body(
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    days_window: int,
    cost_fn: CostFn,
    build_model_section: BuildModelSection,
) -> tuple[list, str]:
    body: list = []
    truncation_note = ""

    dated = {d: m for d, m in totals_by_day_by_model.items() if d != "?"}
    all_days_sorted = sorted(dated.keys(), reverse=True) if dated else []
    shown_days = window_days(dated.keys(), days_window)

    recent_models: dict[str, Bucket] = defaultdict(Bucket)
    for d in shown_days:
        for model, b in dated[d].items():
            recent_models[model].merge(b)

    if recent_models:
        body.extend(build_model_section(dict(recent_models), 0))

    if shown_days:
        if body:
            body.append(_DIV)

        day_rows_data, total_b, total_cost, total_unknown = _aggregate_day_rows(
            dated, shown_days, cost_fn
        )

        for d, b, day_cost, day_unknown in reversed(day_rows_data):
            cost_str = fmt_cost_with_unknown(day_cost, unknown=day_unknown)
            body.append(
                (
                    [
                        d,
                        fmt_count(b.msgs),
                        fmt_count(b.in_),
                        fmt_count(b.out),
                        fmt_count(b.cw),
                        fmt_count(b.cr),
                        cost_str,
                    ],
                    Ansi.NONE,
                    0,
                )
            )

        body.append(_DIV)
        body.append(
            (
                [
                    "TOTAL",
                    fmt_count(total_b.msgs),
                    fmt_count(total_b.in_),
                    fmt_count(total_b.out),
                    fmt_count(total_b.cw),
                    fmt_count(total_b.cr),
                    fmt_cost_with_unknown(total_cost, unknown=total_unknown),
                ],
                Ansi.BOLD_YELLOW,
                0,
            )
        )
        n_days = len(shown_days)
        body.append(
            (
                [
                    "DAILY AVG",
                    fmt_count(total_b.msgs // n_days),
                    fmt_count(total_b.in_ // n_days),
                    fmt_count(total_b.out // n_days),
                    fmt_count(total_b.cw // n_days),
                    fmt_count(total_b.cr // n_days),
                    fmt_cost_with_unknown(total_cost / n_days, unknown=total_unknown),
                ],
                Ansi.BOLD_YELLOW,
                0,
            )
        )

        if days_window > 0 and len(shown_days) < len(all_days_sorted):
            truncation_note = (
                f"  (showing last {len(shown_days)} of "
                f"{len(all_days_sorted)} days with activity; "
                f"use --days 0 to widen)"
            )

    return body, truncation_note


def render_core(  # noqa: PLR0913
    rows: list[dict],
    totals_by_model: dict[str, Bucket],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    days_window: int,
    *,
    cost_fn: CostFn,
    build_model_section: BuildModelSection,
    orphaned: dict | None = None,
) -> str:
    # Two stacked tables: "Total" (all-time per-model + per-project tree) and
    # "Recent" (per-model + per-day, both restricted to the days_window). Each
    # table has internal sections separated by a `├─┼─┤` divider; widths of
    # the trailing six numeric columns are shared across both tables so the
    # numbers line up vertically.
    shared_headers = ["MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    shared_aligns = [">", ">", ">", ">", ">", ">"]
    n_shared = len(shared_headers)

    # === Total table: models (all-time) + project tree ===
    total_headers = ["MODEL / PROJECT", "SESSIONS", "LAST LAUNCH", *shared_headers]
    total_aligns = ["<", ">", "<", *shared_aligns]

    tree_root = build_project_tree(rows)
    display_rows = flatten_tree(tree_root)

    total_body = _build_total_body(
        totals_by_model, tree_root, display_rows, build_model_section, orphaned
    )

    # === Recent table: models (in window) + per-day (in window) + TOTAL ===
    recent_headers = ["MODEL / DATE", *shared_headers]
    recent_aligns = ["<", *shared_aligns]

    recent_body, by_day_truncation_note = _build_recent_body(
        totals_by_day_by_model, days_window, cost_fn, build_model_section
    )

    # === Shared widths for the trailing six numeric columns ===
    shared_widths = compute_shared_widths(
        [(total_headers, total_body, 3), (recent_headers, recent_body, 1)],
        n_shared,
        _DIV,
    )

    lines: list[str] = []
    lines.extend(
        render_table("Total:", total_headers, total_aligns, total_body, 3, shared_widths, _DIV)
    )
    if recent_body:
        recent_title = "Recent:" if days_window == 0 else f"Recent (last {days_window} days):"
        lines.append("")
        lines.extend(
            render_table(
                recent_title, recent_headers, recent_aligns, recent_body, 1, shared_widths, _DIV
            )
        )
        if by_day_truncation_note:
            lines.append(color(by_day_truncation_note, Ansi.DIM))

    return "\n".join(lines)
