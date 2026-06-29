# This file has been edited with the assistance of an AI tool.
"""CLI argument parsing and project-registry loading for the usage-stats subcommands."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class UsageArgsBuilder:
    days_window: int = 30
    verbose: bool = False


@dataclass
class UsageArgs:
    registry_path: Path
    days_window: int = 30
    verbose: bool = False


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


def parse_usage_args(args: list[str], *, usage_line: str, usage_text: str) -> UsageArgs | None:
    """
    Parse `[--days N] <projects.txt>`.

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
        if a == "--days" and i + 1 < len(args):
            days = _parse_days(args[i + 1])
            if days is None:
                return None
            parsed.days_window = days
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

    return UsageArgs(
        registry_path=reg,
        **parsed.__dict__,
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
