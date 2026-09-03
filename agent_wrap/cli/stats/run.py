# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from typing import TYPE_CHECKING

import click

from agent_wrap.cli.params import DateSpec, Regex
from agent_wrap.cli.stats.display import render, render_source_breakdown
from agent_wrap.containers import services
from agent_wrap.domain.stats.models import UsageArgs, WindowError

if TYPE_CHECKING:
    import re
    from datetime import date


@click.command("stats")
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help=(
        "Add a usage-source breakdown table over the same window, splitting totals by how "
        "each record's usage was obtained (native response vs. standard_logging_object "
        "recovery vs. unrecoverable)."
    ),
)
@click.option(
    "-r",
    "--refresh",
    is_flag=True,
    help=(
        "Re-fetch pricing from the providers' pricing pages instead of using the cached "
        "7-day pricing tables."
    ),
)
@click.option(
    "-f",
    "--from",
    "from_date",
    type=DateSpec(),
    metavar="D",
    help="Inclusive lower bound; D is YYYY-MM-DD or -Nd (e.g. -14d).",
)
@click.option(
    "-u",
    "--until",
    "until_date",
    type=DateSpec(),
    metavar="D",
    help="Inclusive upper bound; same format as --from.",
)
@click.option(
    "-d",
    "--days",
    type=click.IntRange(min=0),
    metavar="N",
    help="Span in days; N=0 means unlimited (no day bound).",
)
@click.option(
    "-p",
    "--pattern",
    type=Regex(),
    metavar="P",
    help=(
        "Only show projects whose recorded registry path matches regex P "
        '(e.g. "api", "my-proj", "/home/me/work/").'
    ),
)
@click.pass_context
def stats_command(  # noqa: PLR0913
    ctx: click.Context,
    *,
    verbose: bool,
    refresh: bool,
    from_date: date | None,
    until_date: date | None,
    days: int | None,
    pattern: re.Pattern[str] | None,
) -> None:
    """
    Show token usage stats (reads from .claude/litellm-logs/)

    Print aggregated usage stats from the .claude/litellm-logs/ directory of every
    project in the registry. Output is a per-project table plus a per-model and per-day
    breakdown, both over the same usage window. Models are displayed as
    <provider>/<model>. Day buckets use host-local time by default; override with
    AGENT_DAY_START_UTC.

    At most two of --from, --until and --days may be combined. No flags means the last 28
    days; --from alone means [from, now]; --days N alone means the last N days
    [now-(N-1), now]; --until alone means 28 days ending at until; --days 0 alone means
    all time [open, now].

    Pricing is fetched dynamically per-provider as logs are scanned.

    Projects are recorded by `agent` on each launch -- a project that has never had
    `agent` invoked from it will not appear here.
    """
    dsp = services.display_service

    # Per-value validation lives in the param types; the cross-field window semantics
    # a converter cannot see belong to StatsService.resolve_window. A click callback
    # is no place for it either -- callbacks see ctx.params in an unspecified order.
    window = services.stats_service.resolve_window(
        from_date, until_date, days, days_given=days is not None
    )
    if isinstance(window, WindowError):
        raise click.UsageError(window.message, ctx=ctx)
    from_iso, until_iso = window

    parsed = UsageArgs(
        from_iso=from_iso,
        until_iso=until_iso,
        verbose=verbose,
        pattern=pattern,
        refresh=refresh,
    )

    projects = services.config_service.read_project_paths()
    if not projects:
        dsp.info("no projects recorded yet — launch `agent` once to register a project.")
        ctx.exit(0)

    report = services.stats_service.build_report(projects, parsed)
    if not report.rows and report.orphaned is None:
        if parsed.pattern is not None:
            dsp.info(f"no logs found for any project matching '{parsed.pattern.pattern}'.")
        else:
            dsp.info("no LiteLLM logs found for any registered project.")
        ctx.exit(0)

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
    ctx.exit(0)
