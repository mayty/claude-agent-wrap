# This file has been edited with the assistance of an AI tool.
"""CLI argument parsing and project-registry loading for the usage-stats subcommands."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

# Default span (in days) for the usage window when no explicit count is given.
DEFAULT_DAYS = 28

_RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")


@dataclass
class UsageArgsBuilder:
    from_spec: str | None = None
    until_spec: str | None = None
    days_spec: str | None = None
    verbose: bool = False


@dataclass
class UsageArgs:
    registry_path: Path
    # Resolved inclusive range bounds as ISO dates (YYYY-MM-DD), or None for open.
    from_iso: str | None = None
    until_iso: str | None = None
    verbose: bool = False


def _today() -> datetime:
    return datetime.now().astimezone()


def _parse_days(value: str) -> int | None:
    try:
        days = int(value)
    except ValueError:
        print(f"usage: --days expects an integer, got '{value}'", file=sys.stderr)
        return None
    if days < 0:
        print("usage: --days must be >= 0", file=sys.stderr)
        return None
    return days


def _parse_date_spec(flag: str, value: str):
    """
    Parse a ``--from``/``--until`` value into a ``date``.

    Accepts an absolute ISO date (``YYYY-MM-DD``) or a relative ``-Nd`` offset
    (days only, relative to today). Returns the resolved ``date`` or None on
    error (a usage message is printed to stderr).
    """
    rel = _RELATIVE_DATE_RE.match(value)
    if rel is not None:
        return _today().date() - timedelta(days=int(rel.group(1)))
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()  # noqa: DTZ007
    except ValueError:
        print(
            f"usage: {flag} expects YYYY-MM-DD or -Nd (e.g. -14d), got '{value}'",
            file=sys.stderr,
        )
        return None


def _combine_bounds(from_date, until_date, days_bound, *, days_given: bool):
    """
    Apply the resolution table to already-parsed specs, returning ``(lo, hi)`` dates.

    ``days_bound`` is the positive day count, or None for "no count" (flag absent
    *or* the unlimited ``--days 0``); ``days_given`` distinguishes those two so a
    bare side stays open for ``--days 0`` but defaults to now/DEFAULT_DAYS otherwise.
    """
    today = _today().date()
    span = timedelta(days=days_bound) if days_bound is not None else None
    default_span = timedelta(days=DEFAULT_DAYS)

    if from_date is not None and until_date is not None:
        lo, hi = from_date, until_date
    elif from_date is not None:
        # --from [--days N]: [from, from+N]; [from, open] for --days 0; else [from, now].
        lo = from_date
        hi = from_date + span if span else (None if days_given else today)
    elif until_date is not None:
        # --until [--days N]: [until-N, until]; [open, until] for --days 0; else default span.
        hi = until_date
        lo = (until_date - span if span else None) if days_given else until_date - default_span
    elif days_given:
        # --days N alone: [now-N, now], or all-time for --days 0.
        lo, hi = (today - span, today) if span else (None, None)
    else:
        # No flags: default to the last DEFAULT_DAYS days.
        lo, hi = today - default_span, today
    return lo, hi


def _resolve_range(builder: UsageArgsBuilder) -> tuple[str | None, str | None] | None:
    """
    Resolve the raw ``--from``/``--until``/``--days`` specs into inclusive bounds.

    Returns ``(from_iso, until_iso)`` (each None for an open side) or None on error.
    At most two of the three flags may be given. ``--days 0`` means "unlimited"
    (no count bound). See the resolution table in the command help.
    """
    from_spec, until_spec, days_spec = builder.from_spec, builder.until_spec, builder.days_spec
    if from_spec is not None and until_spec is not None and days_spec is not None:
        print("usage: at most two of --from, --until, --days may be given", file=sys.stderr)
        return None

    from_date = _parse_date_spec("--from", from_spec) if from_spec is not None else None
    if from_spec is not None and from_date is None:
        return None
    until_date = _parse_date_spec("--until", until_spec) if until_spec is not None else None
    if until_spec is not None and until_date is None:
        return None
    days = _parse_days(days_spec) if days_spec is not None else None
    if days_spec is not None and days is None:
        return None
    # A days count of 0 means "unlimited" — it imposes no bound on the open side.
    days_bound = days or None

    lo, hi = _combine_bounds(from_date, until_date, days_bound, days_given=days_spec is not None)

    lo_iso = lo.isoformat() if lo is not None else None
    hi_iso = hi.isoformat() if hi is not None else None
    if lo_iso is not None and hi_iso is not None and lo_iso > hi_iso:
        print("usage: --from date is after --until date", file=sys.stderr)
        return None
    return lo_iso, hi_iso


def parse_usage_args(args: list[str], *, usage_line: str, usage_text: str) -> UsageArgs | None:
    """
    Parse ``[-f|--from D] [-u|--until D] [-d|--days N] [-v] <projects.txt>``.

    `usage_text` is printed for -h/--help; `usage_line` is printed when no
    positional registry path is supplied. Returns None if help was printed or
    on any error.
    """
    parsed = UsageArgsBuilder()
    positional: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(usage_text, file=sys.stderr)
            return None
        if a in ("-v", "--verbose"):
            parsed.verbose = True
            i += 1
            continue
        if a in ("-d", "--days") and i + 1 < len(args):
            parsed.days_spec = args[i + 1]
            i += 2
            continue
        if a in ("-f", "--from") and i + 1 < len(args):
            parsed.from_spec = args[i + 1]
            i += 2
            continue
        if a in ("-u", "--until") and i + 1 < len(args):
            parsed.until_spec = args[i + 1]
            i += 2
            continue
        positional.append(a)
        i += 1

    if not positional:
        print(usage_line, file=sys.stderr)
        return None

    reg = Path(positional[0])
    if not reg.is_file():
        print(f"usage: registry not found at {reg}", file=sys.stderr)
        return None

    resolved = _resolve_range(parsed)
    if resolved is None:
        return None
    from_iso, until_iso = resolved

    return UsageArgs(
        registry_path=reg,
        from_iso=from_iso,
        until_iso=until_iso,
        verbose=parsed.verbose,
    )


def load_projects(reg: Path) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []
    for line in reg.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(Path(s))
    return out
