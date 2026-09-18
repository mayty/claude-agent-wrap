# This file has been edited with the assistance of an AI tool.
"""The `stats` subcommand — aggregate token usage stats from LiteLLM logs."""

from typing import TYPE_CHECKING

import click

from agent_wrap.cli.params import DateSpec, Regex
from agent_wrap.cli.stats.display import render
from agent_wrap.containers import services
from agent_wrap.domain.stats.models import UsageArgs, WindowError

if TYPE_CHECKING:
    import re
    from datetime import date

    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.logs.models import IndexLag


@click.command("stats")
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
    refresh: bool,
    from_date: date | None,
    until_date: date | None,
    days: int | None,
    pattern: re.Pattern[str] | None,
) -> None:
    """
    Show token usage stats (reads the request index)

    Print aggregated usage stats for every project in the registry. Output is a
    per-project table plus a per-model and per-day breakdown, both over the same usage
    window. Models are displayed as <provider>/<model>. Day buckets use host-local time
    by default; override with AGENT_DAY_START_UTC.

    At most two of --from, --until and --days may be combined. No flags means the last 28
    days; --from alone means [from, now]; --days N alone means the last N days
    [now-(N-1), now]; --until alone means 28 days ending at until; --days 0 alone means
    all time [open, now].

    Totals come from the request index, not from the log files -- so they cover exactly
    what has been indexed. `agent logs` fills the index as requests arrive and
    `agent reindex` catches it up on demand; this command reports when the two have
    drifted apart, and never writes either.

    Pricing is fetched dynamically per-provider as the totals are computed.

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
        pattern=pattern,
        refresh=refresh,
    )

    projects = services.config_service.read_project_paths()
    if not projects:
        dsp.info("no projects recorded yet — launch `agent` once to register a project.")
        ctx.exit(0)

    report = services.stats_service.build_report(projects, parsed)
    lag = services.logs_service.index_lag()
    if not report.rows and report.orphaned is None:
        if parsed.pattern is not None:
            dsp.info(f"no logs found for any project matching '{parsed.pattern.pattern}'.")
        else:
            dsp.info("no indexed requests found for any registered project.")
        # Reported on this path too, and it matters most here: an empty report on a host
        # whose logs have simply never been indexed reads as "you have spent nothing",
        # which is the one wrong conclusion the warning exists to prevent.
        _warn_if_stale(lag, dsp)
        ctx.exit(0)

    dsp.show(
        render(
            report.rows,
            report.totals_by_day_by_model,
            parsed.from_iso,
            parsed.until_iso,
            orphaned=report.orphaned,
            display=dsp,
        )
    )

    # Footnote any successful requests whose usage was never recorded.
    if report.unrecorded:
        dsp.warning(
            f"{report.unrecorded} successful request(s) had unrecorded usage and "
            "contribute $0 to the totals above (response logged without a usage "
            "block). Cost is understated by their unknown amount."
        )
    # Last, so the command to run is the final line on screen.
    _warn_if_stale(lag, dsp)
    ctx.exit(0)


def _warn_if_stale(lag: IndexLag, dsp: DisplayService) -> None:
    """
    Say so when the index is behind the log files, and name the fix.

    Never silently absorbed and never worked around: this command reads the index and
    only the index, so a request the index has not seen is missing from the totals
    above. Saying which sessions are behind and what to run is the whole remedy —
    reading the files here to fill the gap is what the index exists to stop.
    """
    if not lag.is_stale:
        return
    dsp.warning(
        f"{lag.behind} of {lag.total} session(s) are behind the log files; totals may "
        "understate spend. Run: agent reindex"
    )
