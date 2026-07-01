# This file has been created with the assistance of an AI tool.
"""Terminal rendering for the stats command."""

from __future__ import annotations

from typing import Any

from agent_wrap.cli.stats.render import range_label, render_core
from agent_wrap.cli.stats.tree import DisplayRow, build_project_tree, flatten_tree
from agent_wrap.constants import USAGE_SOURCES
from agent_wrap.domain.pricing.cost_format import fmt_cost_with_unknown
from agent_wrap.domain.pricing.models import Bucket
from agent_wrap.lib.console import Ansi
from agent_wrap.lib.format import fmt_count
from agent_wrap.lib.table import RowItem, compute_shared_widths, render_table


def _model_display_rows(totals_by_model: dict[str, Bucket]) -> list[DisplayRow]:
    """
    Render the per-model breakdown as a provider/model tree.

    Reuses the same trie machinery as the project tree by treating each
    `provider/model` key as a path that `Path(...).parts` splits on `/`.
    Models carry no session/launch data, so those columns are left blank by
    the callers; only the token/cost columns of each DisplayRow are used.
    """
    rows = [
        {
            "path": model,
            "exists": True,
            "sessions": 0,
            "last_ts": None,
            "total": bucket,
            "cost": None if bucket.cost_unknown else bucket.cost,
        }
        for model, bucket in totals_by_model.items()
    ]
    return flatten_tree(build_project_tree(rows))


def _build_model_section(totals_by_model: dict[str, Bucket], leading_blanks: int) -> list[RowItem]:
    """
    Build the per-model breakdown body rows (provider/model tree).

    `leading_blanks` empty cells follow the label to skip the SESSIONS /
    LAST LAUNCH columns in the Total table (2) versus the Recent table (0).
    """
    blanks = [""] * leading_blanks
    body: list[RowItem] = []
    for dr in _model_display_rows(totals_by_model):
        style = Ansi.DIM if dr.is_structural else Ansi.NONE
        body.append(
            (
                [
                    dr.label,
                    *blanks,
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
    return body


def render(
    rows: list[dict[str, Any]],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    from_iso: str | None,
    until_iso: str | None,
    orphaned: dict[str, Any] | None = None,
) -> str:
    # Per-request cost is baked into `Bucket.cost` during the scan; the bucket's
    # `cost_unknown` flag (set when a billable request had no known price) is the
    # authoritative "?" signal — a 0.0 cost without that flag is a known zero.
    return render_core(
        rows,
        totals_by_day_by_model,
        from_iso,
        until_iso,
        cost_fn=lambda _model, b: (b.cost, b.cost_unknown),
        build_model_section=_build_model_section,
        orphaned=orphaned,
    )


def render_source_breakdown(
    totals_by_source: dict[str, dict[str, Bucket]],
    from_iso: str | None,
    until_iso: str | None,
) -> str:
    """
    Render the verbose "usage source breakdown" table for the selected window.

    One row per usage source (native / standard_logging_object / unrecoverable,
    see :func:`_usage_source`) showing how much of the reported totals came
    straight from responses vs. were recovered from LiteLLM's standard logging
    object fallback vs. were lost — so a reader can judge how far the headline
    cost depends on the recovery path. The source dict is already restricted to
    the window at scan time; rendered standalone (its own column widths) since
    it prints after the main tables.

    *totals_by_source* is ``{source: {model: Bucket}}``. Model buckets are merged
    within each source to get per-source totals. Cost reads ``Bucket.cost`` /
    ``cost_unknown`` directly, valid here because ``price_buckets`` has already
    priced the model-keyed buckets (same basis as :func:`render`).
    Returns "" when no source has activity in the window.
    """
    merged: dict[str, Bucket] = {}
    for source, by_model in totals_by_source.items():
        src_bucket = merged.setdefault(source, Bucket())
        for b in by_model.values():
            src_bucket.merge(b)

    headers = ["SOURCE", "MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST"]
    aligns = ["<", ">", ">", ">", ">", ">", ">"]
    div = "__div__"

    def _row(label: str, b: Bucket, style: Ansi) -> RowItem:
        return (
            [
                label,
                fmt_count(b.msgs),
                fmt_count(b.in_),
                fmt_count(b.out),
                fmt_count(b.cw),
                fmt_count(b.cr),
                fmt_cost_with_unknown(b.cost, unknown=b.cost_unknown),
            ],
            style,
            0,
        )

    body: list[RowItem] = []
    total = Bucket()
    for source in USAGE_SOURCES:
        b = merged.get(source)
        # Unrecoverable rows carry msgs but zero tokens; the msgs guard keeps them
        # (intentionally surfaced) while dropping sources with no activity at all.
        if b is None or b.msgs == 0:
            continue
        total.merge(b)
        body.append(_row(source, b, Ansi.NONE))

    if not body:
        return ""

    body.append(div)
    body.append(_row("TOTAL", total, Ansi.BOLD_YELLOW))

    shared_widths = compute_shared_widths([(headers, body, 1)], 6)
    title = f"Usage source breakdown ({range_label(from_iso, until_iso)}):"
    return "\n".join(render_table(title, headers, aligns, body, 1, shared_widths))
