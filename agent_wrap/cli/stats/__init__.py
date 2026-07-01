# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from agent_wrap.cli.stats.display import render, render_source_breakdown
from agent_wrap.constants import AGENT_LAUNCHES_DIR
from agent_wrap.containers import services
from agent_wrap.domain.stats.usage_args import parse_usage_args

if TYPE_CHECKING:
    from agent_wrap.domain.stats.models import UsageArgs

USAGE = "[-v|--verbose] [-f|--from D] [-u|--until D] [-d|--days N]"
SUMMARY = "Show token usage stats (reads from .claude/litellm-logs/)"

_USAGE_TEXT = (
    "Usage: agent stats [-v|--verbose] [-f|--from D] [-u|--until D] [-d|--days N] <projects.txt>\n\n"
    "Reads a list of project paths (one per line) and prints aggregated\n"
    "usage stats from each project's .claude/litellm-logs/ directories.\n\n"
    "Output is a per-project table plus a per-model and per-day breakdown,\n"
    "both over the same usage window. Models are displayed as <provider>/<model>.\n"
    "Day buckets use host-local time.\n\n"
    "Selection range (at most two of --from/--until/--days may be combined):\n"
    "  -f, --from D    inclusive lower bound; D is YYYY-MM-DD or -Nd (e.g. -14d)\n"
    "  -u, --until D   inclusive upper bound; same format as --from\n"
    "  -d, --days N    span in days; N=0 means unlimited (no day bound)\n"
    "Defaults: no flags → last 28 days; --from alone → [from, now];\n"
    "--days N alone → last N days [now-(N-1), now]; --until alone → 28 days\n"
    "ending at until; --days 0 alone → all time [open, now].\n\n"
    "-v/--verbose adds a usage-source breakdown table over the same window,\n"
    "splitting totals by how each record's usage was obtained\n"
    "(native response vs. standard_logging_object recovery vs. unrecoverable).\n\n"
    "Pricing is fetched dynamically per-provider as logs are scanned.\n\n"
    "Projects are recorded by `agent` on each launch — a project that\n"
    "has never had `agent` invoked from it will not appear here."
)

_USAGE_LINE = (
    "Usage: agent stats [-v|--verbose] [-f|--from D] [-u|--until D] [-d|--days N] <projects.txt>"
)


def _parse_usage_args(args: list[str]) -> UsageArgs | None:
    return parse_usage_args(args, usage_line=_USAGE_LINE, usage_text=_USAGE_TEXT)


def run(args: list[str]) -> int:
    projects_file = AGENT_LAUNCHES_DIR / "projects.txt"

    # Inject tool-dir-derived paths into the args stream
    injected = [str(projects_file), *args]
    parsed = _parse_usage_args(injected)
    if parsed is None:
        return 1 if args and args[0] not in ("-h", "--help") else 0

    projects = services.stats_service.load_projects(parsed.registry_path)
    if not projects:
        print(
            "usage: no projects recorded yet — launch `agent` once to register a project.",
            file=sys.stderr,
        )
        return 0

    stats = services.stats_service

    # Scan every logs dir — projects and orphaned alike — in one pass up front.
    orphaned_dirs = stats.orphaned_log_dirs(projects)
    project_log_dirs = [p / ".claude" / "litellm-logs" for p in projects]
    scan_cache = stats.scan_log_dirs(
        [*project_log_dirs, *orphaned_dirs],
        from_iso=parsed.from_iso,
        until_iso=parsed.until_iso,
    )

    rows, totals_by_model, totals_by_day_by_model, totals_by_source = stats.aggregate_projects(
        projects,
        from_iso=parsed.from_iso,
        until_iso=parsed.until_iso,
        scan_cache=scan_cache,
    )

    # Filter out projects with no logs
    rows = [r for r in rows if r["sessions"] > 0]

    # Logs left behind by deleted projects / stale registry entries.
    orphaned = stats.aggregate_orphaned(
        projects,
        totals_by_model,
        totals_by_day_by_model,
        totals_by_source,
        from_iso=parsed.from_iso,
        until_iso=parsed.until_iso,
        scan_cache=scan_cache,
    )

    if not rows and orphaned is None:
        print("usage: no LiteLLM logs found for any registered project.", file=sys.stderr)
        return 0

    print(
        render(
            rows,
            totals_by_day_by_model,
            parsed.from_iso,
            parsed.until_iso,
            orphaned=orphaned,
        )
    )

    if parsed.verbose:
        breakdown = render_source_breakdown(totals_by_source, parsed.from_iso, parsed.until_iso)
        if breakdown:
            print()
            print(breakdown)

    # Footnote any successful requests whose usage was never recorded.
    unrecorded = sum(b.unrecorded for b in totals_by_model.values())
    if unrecorded:
        print(
            f"\nnote: {unrecorded} successful request(s) had unrecorded usage and "
            "contribute $0 to the totals above (response logged without a usage "
            "block). Cost is understated by their unknown amount.",
            file=sys.stderr,
        )
    return 0
