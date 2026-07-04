# This file has been edited with the assistance of an AI tool.
"""CLI argument parsing and project-registry loading for the usage-stats subcommands."""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from agent_wrap.domain.stats.constants import DEFAULT_DAYS, RELATIVE_DATE_RE, VALUE_FLAGS
from agent_wrap.domain.stats.models import UsageArgs


def _today() -> datetime:
    return datetime.now().astimezone()


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


def _parse_date_spec(value: str):
    """
    Argparse ``type`` for ``--from``/``--until``: parse a value into a ``date``.

    Accepts an absolute ISO date (``YYYY-MM-DD``) or a relative ``-Nd`` offset
    (days only, relative to today). Raises ``ArgumentTypeError`` on a malformed
    value; argparse prefixes the message with the offending ``argument`` name.
    """
    rel = RELATIVE_DATE_RE.match(value)
    if rel is not None:
        return _today().date() - timedelta(days=int(rel.group(1)))
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        msg = f"expects YYYY-MM-DD or -Nd (e.g. -14d), got '{value}'"
        raise argparse.ArgumentTypeError(msg) from None


def _shift(d: date, span: timedelta, *, sign: int) -> date:
    """
    ``date ± span``, clamped to ``[date.min, date.max]`` instead of raising OverflowError.

    The unlimited ``--days 0`` case uses ``timedelta.max`` as its span; adding or
    subtracting that from a real date overflows, so saturate to the open-side
    sentinel (``date.max`` for a forward shift, ``date.min`` for a backward one).
    """
    try:
        return d + sign * span
    except OverflowError:
        return date.max if sign > 0 else date.min


def _combine_bounds(
    from_date: date | None,
    until_date: date | None,
    days_bound: int | None,
    *,
    days_given: bool,
) -> tuple[date, date]:
    """
    Apply the resolution table to already-parsed specs, returning ``(lo, hi)`` dates.

    ``days_bound`` is the positive day count, or None for "no count" (flag absent
    *or* the unlimited ``--days 0``); ``days_given`` distinguishes those two so a
    bare side stays open for ``--days 0`` but defaults to now/DEFAULT_DAYS otherwise.

    Open sides are returned as the ``date.min`` / ``date.max`` sentinels;
    :func:`_resolve_range` maps those back to None at the ISO boundary.
    """
    today = _today().date()
    # Bounds are inclusive on both sides, so an N-day window offsets by N-1.
    # ``--days 0`` (days_given but no count) means "unlimited" — timedelta.max
    # saturates the bare side to an open sentinel via _shift.
    if days_given:
        span = timedelta(days=days_bound - 1) if days_bound else timedelta.max
    else:
        span = timedelta(days=DEFAULT_DAYS - 1)

    if from_date is not None and until_date is not None:
        return from_date, until_date
    if from_date is not None:
        # --from [--days N]: [from, from+(N-1)]; [from, open] for --days 0; else [from, now].
        return from_date, _shift(from_date, span, sign=1) if days_given else today
    if until_date is not None:
        # --until [--days N]: [until-(N-1), until]; [open, until] for --days 0; else default span.
        return _shift(until_date, span, sign=-1), until_date
    # No --from/--until: the last N (or DEFAULT_DAYS) inclusive days [now-(N-1), now],
    # or all-time [open, now] for --days 0.
    return _shift(today, span, sign=-1), today


def _resolve_range(
    from_date: date | None, until_date: date | None, days: int | None, *, days_given: bool
) -> tuple[str | None, str | None] | None:
    """
    Resolve the parsed ``--from``/``--until``/``--days`` values into inclusive bounds.

    ``from_date``/``until_date`` are ``date`` or None; ``days`` is an int or None;
    ``days_given`` says whether ``--days`` was passed (to tell ``--days 0`` from
    "absent"). Returns ``(from_iso, until_iso)`` (each None for an open side) or
    None on error. At most two of the three flags may be given. ``--days 0``
    means "unlimited" (no count bound). See the resolution table in the help.
    """
    if from_date is not None and until_date is not None and days_given:
        print("usage: at most two of --from, --until, --days may be given", file=sys.stderr)
        return None

    # A days count of 0 means "unlimited" — it imposes no bound on the open side.
    days_bound = days or None

    lo, hi = _combine_bounds(from_date, until_date, days_bound, days_given=days_given)

    # The date.min / date.max sentinels mark open sides; collapse them back to None.
    lo_iso = None if lo == date.min else lo.isoformat()
    hi_iso = None if hi == date.max else hi.isoformat()
    if lo_iso is not None and hi_iso is not None and lo_iso > hi_iso:
        print("usage: --from date is after --until date", file=sys.stderr)
        return None
    return lo_iso, hi_iso


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
    parser.add_argument("registry")
    return parser


def parse_usage_args(args: list[str], *, usage_line: str, usage_text: str) -> UsageArgs | None:
    """
    Parse ``[-f|--from D] [-u|--until D] [-d|--days N] [-v] <projects.txt>``.

    `usage_text` is rendered as the parser description for -h/--help; `usage_line`
    becomes the usage prefix. Returns None if help was printed or on any error
    (the caller treats None as "stop"). Per-value validation (date/days formats)
    happens in the argparse ``type=`` converters; :func:`_resolve_range` applies
    the cross-field semantics that a per-value converter can't see.
    """
    parser = build_parser()
    parser.usage = usage_line.removeprefix("Usage: ")
    parser.description = usage_text

    try:
        ns = parser.parse_args(_glue_dash_values(args))
    except SystemExit:
        # argparse already printed help (-h) or the error; both map to "stop".
        return None

    reg = Path(ns.registry)
    if not reg.is_file():
        print(f"usage: registry not found at {reg}", file=sys.stderr)
        return None

    resolved = _resolve_range(ns.from_date, ns.until_date, ns.days, days_given=ns.days is not None)
    if resolved is None:
        return None
    from_iso, until_iso = resolved

    return UsageArgs(
        registry_path=reg,
        from_iso=from_iso,
        until_iso=until_iso,
        verbose=ns.verbose,
    )
