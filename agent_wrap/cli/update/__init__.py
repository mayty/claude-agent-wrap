# This file has been edited with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

from __future__ import annotations

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

USAGE = ""
SUMMARY = "Pull upstream updates"


def run(args: list[str]) -> int:
    """Execute the `update` subcommand."""
    ns = parse_or_code(make_parser("update", usage_summary=USAGE), args)
    if isinstance(ns, int):
        return ns
    return services.update_service.apply()
