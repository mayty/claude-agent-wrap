# This file has been edited with the assistance of an AI tool.
"""The `legacy_stats` subcommand — aggregate Claude Code usage stats from session data."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from typing import TYPE_CHECKING

from agent_wrap.lib.buckets import Bucket
from agent_wrap.lib.console import Ansi
from agent_wrap.lib.format import fmt_cost, fmt_count, parse_ts
from agent_wrap.lib.models import normalize_model
from agent_wrap.lib.render import render_core
from agent_wrap.lib.tree import build_project_tree, flatten_tree
from agent_wrap.lib.usage_args import (
    UsageArgs,
    load_projects,
    parse_usage_args,
)
from agent_wrap.providers import get_provider

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

# Re-exported for tests (test_legacy_stats imports these from this module).
__all__ = [
    "Bucket",
    "build_project_tree",
    "cost_for",
    "flatten_tree",
    "fmt_cost",
    "fmt_count",
    "load_projects",
    "normalize_model",
    "render",
    "run",
    "scan_project",
]

USAGE = "[--days N]"
SUMMARY = "Show token usage stats (reads from .claude/sessions/*.jsonl)"


def cost_for(model: str, usage: dict, prices: dict) -> float | None:
    # Synthetic / placeholder records contribute no tokens; treat them as
    # zero-cost rather than poisoning the project's total with "?".
    if not any(usage.values()):
        return 0.0
    key = normalize_model(model)
    if key is None:
        return None
    p = prices.get(key)
    if p is None:
        return None
    return (
        usage["in"] * p["in"] / 1_000_000
        + usage["out"] * p["out"] / 1_000_000
        + usage["cw_5m"] * p["cw_5m"] / 1_000_000
        + usage["cw_1h"] * p["cw_1h"] / 1_000_000
        + usage["cr"] * p["cr"] / 1_000_000
    )


def _process_record(
    rec: dict,
    seen_message_ids: set[str],
    buckets: dict[str, dict[str, Bucket]],
) -> datetime | None:
    """Process a single JSONL record into buckets. Returns the timestamp if valid."""
    ts = parse_ts(rec.get("timestamp"))
    if rec.get("type") != "assistant":
        return ts
    msg = rec.get("message") or {}
    usage = msg.get("usage")
    if not usage:
        return ts
    msg_id = msg.get("id")
    if msg_id:
        if msg_id in seen_message_ids:
            return ts
        seen_message_ids.add(msg_id)
    model = msg.get("model") or "?"
    day_key = ts.astimezone().strftime("%Y-%m-%d") if ts is not None else "?"
    buckets[day_key][model].add(usage)
    return ts


def scan_project(
    path: Path,
    seen_message_ids: set[str],
) -> tuple[int, datetime | None, dict[str, dict[str, Bucket]], bool]:
    """
    Return (session_count, last_ts, per_day_per_model_buckets, exists).

    The buckets dict is keyed `day → model → Bucket`, where `day` is either a
    `YYYY-MM-DD` string in host-local time or `"?"` for records whose
    timestamp was missing or unparseable. `exists` is False when the project
    directory or its sessions dir is gone.

    `seen_message_ids` is a global set used to dedupe assistant records whose
    `message.id` has already been counted. Claude Code replays prior turns
    into new session files when a session is resumed/compacted/forked, so the
    same `msg_bdrk_…` id appears in multiple `.jsonl` files; Bedrock bills the
    underlying call once. The set is mutated in place across all projects.
    """
    sessions_dir = path / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return 0, None, {}, False

    # Top-level *.jsonl is one file per resumable conversation; subagent
    # invocations live in `<sid>/subagents/*.jsonl` under each session and
    # carry their own billable assistant turns. We scan both, but the
    # session-count column reflects only top-level files (the user-facing
    # "session" granularity).
    top_files = sorted(sessions_dir.glob("*.jsonl"))
    files = sorted(sessions_dir.rglob("*.jsonl"))
    last_ts: datetime | None = None
    buckets: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))

    for f in files:
        try:
            with f.open("r", encoding="utf-8", errors="replace") as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = _process_record(rec, seen_message_ids, buckets)
                    if ts is not None and (last_ts is None or ts > last_ts):
                        last_ts = ts
        except OSError:
            continue

    return len(top_files), last_ts, {d: dict(m) for d, m in buckets.items()}, True


def _make_build_model_section(prices: dict):
    """
    Build a flat, cost-descending per-model section builder bound to `prices`.

    Models are listed as bare ids (no provider prefix), sorted by cost
    descending, with each cost formatted directly via `cost_for`/`fmt_cost`.
    `leading_blanks` empty cells follow the label to skip the SESSIONS /
    LAST LAUNCH columns in the Total table (2) versus the Recent table (0).
    """

    def build(totals_by_model: dict[str, Bucket], leading_blanks: int) -> list:
        blanks = [""] * leading_blanks
        ordered = sorted(
            totals_by_model.items(),
            key=lambda kv: cost_for(kv[0], kv[1].usage_dict(), prices) or 0.0,
            reverse=True,
        )
        body: list = []
        for model, b in ordered:
            c = cost_for(model, b.usage_dict(), prices)
            body.append(
                (
                    [
                        model,
                        *blanks,
                        fmt_count(b.msgs),
                        fmt_count(b.in_),
                        fmt_count(b.out),
                        fmt_count(b.cw),
                        fmt_count(b.cr),
                        fmt_cost(c),
                    ],
                    Ansi.NONE,
                    0,
                )
            )
        return body

    return build


def render(
    rows: list[dict],
    totals_by_model: dict[str, Bucket],
    totals_by_day_by_model: dict[str, dict[str, Bucket]],
    prices: dict,
    days_window: int,
) -> str:
    # Legacy costs lazily from a flat pricing dict, so cost/unknown is computed
    # per (model, bucket) via `cost_for` (None => unknown, rendered as "?").
    def cost_fn(model: str, b: Bucket) -> tuple[float, bool]:
        c = cost_for(model, b.usage_dict(), prices)
        return (c or 0.0, c is None)

    return render_core(
        rows,
        totals_by_model,
        totals_by_day_by_model,
        days_window,
        cost_fn=cost_fn,
        build_model_section=_make_build_model_section(prices),
    )


_USAGE_TEXT = (
    "Usage: agent legacy_stats [--days N] <projects.txt>\n\n"
    "Reads a list of project paths (one per line) and prints aggregated\n"
    "usage stats from each project's .claude/sessions/*.jsonl files.\n\n"
    "Output is a per-project table plus per-model and per-day breakdowns.\n"
    "Day buckets use host-local time. --days N limits the per-day section\n"
    "to the most recent N calendar days (default 30; use 0 to show all).\n\n"
    "Pricing is fetched from the default provider (litellm-bedrock).\n\n"
    "Projects are recorded by `agent` on each launch — a project that\n"
    "has never had `agent` invoked from it will not appear here."
)


_USAGE_LINE = "Usage: agent legacy_stats [--days N] <projects.txt>"


def _parse_usage_args(args: list[str]) -> UsageArgs | None:
    """Parse CLI arguments. Returns None if help was printed or an error occurred."""
    return parse_usage_args(args, usage_line=_USAGE_LINE, usage_text=_USAGE_TEXT)


def _collect_project_rows(
    projects: list[Path],
    prices: dict,
) -> tuple[list[dict], dict[str, Bucket], dict[str, dict[str, Bucket]]]:
    """Scan all projects and return (rows, totals_by_model, totals_by_day_by_model)."""
    rows: list[dict] = []
    totals_by_model: dict[str, Bucket] = defaultdict(Bucket)
    totals_by_day_by_model: dict[str, dict[str, Bucket]] = defaultdict(lambda: defaultdict(Bucket))
    seen_message_ids: set[str] = set()

    for path in projects:
        sessions, last_ts, by_day, exists = scan_project(path, seen_message_ids)
        total = Bucket()
        proj_cost: float = 0.0
        proj_unknown = False
        for day, by_model in by_day.items():
            for model, b in by_model.items():
                total.merge(b)
                totals_by_model[model].merge(b)
                totals_by_day_by_model[day][model].merge(b)
                c = cost_for(model, b.usage_dict(), prices)
                if c is None:
                    proj_unknown = True
                else:
                    proj_cost += c
        rows.append(
            {
                "path": path,
                "exists": exists,
                "sessions": sessions,
                "last_ts": last_ts,
                "total": total,
                "cost": None if proj_unknown else proj_cost,
            }
        )

    rows.sort(key=lambda r: r["cost"] if r["cost"] is not None else -1.0, reverse=True)
    return rows, dict(totals_by_model), {d: dict(m) for d, m in totals_by_day_by_model.items()}


def run(args: list[str], tool_dir: Path) -> int:
    """
    Execute the `legacy_stats` subcommand.

    Constructs the project-registry path from ``tool_dir``, injects it into
    the argument stream, and runs the core usage logic.
    """
    projects_file = tool_dir / ".agent-launches" / "projects.txt"

    # Inject the tool-dir-derived registry path into the args stream so
    # _parse_usage_args can find the positional registry path.
    injected = [str(projects_file), *args]
    parsed = _parse_usage_args(injected)
    if parsed is None:
        return 1 if args and args[0] not in ("-h", "--help") else 0

    projects = load_projects(parsed.registry_path)
    if not projects:
        print(
            "usage: no projects recorded yet — launch `agent` once to register a project.",
            file=sys.stderr,
        )
        return 0

    # Fetch pricing from the default provider
    try:
        provider = get_provider("litellm-bedrock")
        prices = provider.get_pricing()
    except Exception:  # noqa: BLE001
        prices = {}

    rows, totals_by_model, totals_by_day_by_model = _collect_project_rows(projects, prices)

    print(
        render(
            rows,
            totals_by_model,
            totals_by_day_by_model,
            prices,
            parsed.days_window,
        )
    )
    return 0
