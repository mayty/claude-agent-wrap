# This file has been edited with the assistance of an AI tool.
"""CLI argument parsing for the usage-stats subcommand."""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from agent_wrap.cli.stats.constants import RELATIVE_DATE_RE, VALUE_FLAGS
from agent_wrap.constants import DAY_START_HOURS
from agent_wrap.containers import services
from agent_wrap.domain.stats.models import UsageArgs, WindowError
from agent_wrap.lib.daytime import get_day

if TYPE_CHECKING:
    from datetime import date

    from agent_wrap.domain.display.service import DisplayService


def _parse_days(value: str) -> int:
    """
    Argparse ``type`` for ``--days``: a non-negative integer.

    ``0`` is valid and means "unlimited" (the no-bound case is derived later).
    Raises ``ArgumentTypeError`` on a non-integer or negative value; argparse
    prefixes the message with ``argument -d/--days:`` and exits with an error.
    """
    try:
        days = int(value)
    except ValueError:
        msg = f"expects an integer, got '{value}'"
        raise argparse.ArgumentTypeError(msg) from None
    if days < 0:
        msg = "must be >= 0"
        raise argparse.ArgumentTypeError(msg)
    return days


def _parse_date_spec(value: str) -> date:
    """
    Argparse ``type`` for ``--from``/``--until``: parse a value into a ``date``.

    Accepts an absolute ISO date (``YYYY-MM-DD``) or a relative ``-Nd`` offset
    (days only, relative to today). Raises ``ArgumentTypeError`` on a malformed
    value; argparse prefixes the message with the offending ``argument`` name.
    """
    rel = RELATIVE_DATE_RE.match(value)
    if rel is not None:
        today = get_day(services.stats_service.now_utc(), DAY_START_HOURS)
        return today - timedelta(days=int(rel.group(1)))
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        msg = f"expects YYYY-MM-DD or -Nd (e.g. -14d), got '{value}'"
        raise argparse.ArgumentTypeError(msg) from None


def _glue_dash_values(args: list[str]) -> list[str]:
    """
    Rewrite ``--from -14d`` to ``--from=-14d`` for the date flags.

    The ``-Nd`` relative-date form looks like an option to argparse, which then
    refuses to consume it as ``--from``'s value. Joining the flag and its value
    with ``=`` sidesteps that — argparse always reads the right-hand side of
    ``--flag=value`` literally. Only the date flags are glued; ``--days`` takes
    integers (argparse already accepts a leading-dash negative there) and a bare
    trailing flag is left alone so the "expected one argument" error still fires.
    """
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in VALUE_FLAGS and i + 1 < len(args) and args[i + 1].startswith("-"):
            out.append(f"{a}={args[i + 1]}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def build_parser() -> argparse.ArgumentParser:
    """Return an ``ArgumentParser`` for ``agent stats`` (no usage text set)."""
    parser = argparse.ArgumentParser(
        prog="agent stats",
        allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-f", "--from", dest="from_date", type=_parse_date_spec, metavar="D")
    parser.add_argument("-u", "--until", dest="until_date", type=_parse_date_spec, metavar="D")
    parser.add_argument("-d", "--days", dest="days", type=_parse_days, metavar="N")
    parser.add_argument(
        "-p",
        "--pattern",
        dest="pattern",
        metavar="P",
        help="only show projects whose recorded registry path matches regex P",
    )
    return parser


def parse_usage_args(
    args: list[str],
    *,
    usage_line: str,
    usage_text: str,
    display: DisplayService,
) -> UsageArgs | None:
    """
    Parse ``[-v] [-p P] [-f|--from D] [-u|--until D] [-d|--days N]``.

    `usage_text` is rendered as the parser description for -h/--help; `usage_line`
    becomes the usage prefix. Returns None if help was printed or on any error
    (the caller treats None as "stop"). Per-value validation (date/days formats)
    happens in the argparse ``type=`` converters; the cross-field window semantics
    a per-value converter cannot see belong to ``StatsService.resolve_window``.
    """
    parser = build_parser()
    parser.usage = usage_line.removeprefix("Usage: ")
    parser.description = usage_text

    try:
        ns = parser.parse_args(_glue_dash_values(args))
    except SystemExit:
        # argparse already printed help (-h) or the error; both map to "stop".
        return None

    window = services.stats_service.resolve_window(
        ns.from_date, ns.until_date, ns.days, days_given=ns.days is not None
    )
    if isinstance(window, WindowError):
        display.error(window.message)
        return None
    from_iso, until_iso = window

    compiled_pattern: re.Pattern[str] | None = None
    if ns.pattern is not None:
        try:
            compiled_pattern = re.compile(ns.pattern)
        except re.error as exc:
            display.error(f"usage: invalid regex pattern: {exc}")
            return None

    return UsageArgs(
        from_iso=from_iso,
        until_iso=until_iso,
        verbose=ns.verbose,
        pattern=compiled_pattern,
    )
