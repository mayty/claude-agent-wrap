# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from agent_wrap.cli.stats.constants import USAGE_LINE, USAGE_TEXT
from agent_wrap.cli.stats.display import render, render_source_breakdown
from agent_wrap.cli.stats.usage_args import parse_usage_args
from agent_wrap.containers import services

USAGE = "[-v|--verbose] [-r|--refresh] [-p|--pattern P] [-f|--from D] [-u|--until D] [-d|--days N]"
SUMMARY = "Show token usage stats (reads from .claude/litellm-logs/)"


def run(args: list[str]) -> int:
    dsp = services.display_service
    parsed = parse_usage_args(args, usage_line=USAGE_LINE, usage_text=USAGE_TEXT, display=dsp)
    if parsed is None:
        return 1 if args and args[0] not in ("-h", "--help") else 0

    projects = services.config_service.read_project_paths()
    if not projects:
        dsp.info("no projects recorded yet — launch `agent` once to register a project.")
        return 0

    report = services.stats_service.build_report(projects, parsed)
    if not report.rows and report.orphaned is None:
        if parsed.pattern is not None:
            dsp.info(f"no logs found for any project matching '{parsed.pattern.pattern}'.")
        else:
            dsp.info("no LiteLLM logs found for any registered project.")
        return 0

    dsp.info(
        render(
            report.rows,
            report.totals_by_day_by_model,
            parsed.from_iso,
            parsed.until_iso,
            orphaned=report.orphaned,
            display=dsp,
        )
    )

    if parsed.verbose:
        breakdown = render_source_breakdown(
            report.totals_by_source, parsed.from_iso, parsed.until_iso, display=dsp
        )
        if breakdown:
            dsp.newline()
            dsp.info(breakdown)

    # Footnote any successful requests whose usage was never recorded.
    if report.unrecorded:
        dsp.warning(
            f"{report.unrecorded} successful request(s) had unrecorded usage and "
            "contribute $0 to the totals above (response logged without a usage "
            "block). Cost is understated by their unknown amount."
        )
    return 0
