# This file has been edited with the assistance of an AI tool.
"""The `create` subcommand — scaffolds a Dockerfile.agent."""

from __future__ import annotations

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

USAGE = ""
SUMMARY = "Scaffold Dockerfile.agent"


def run(args: list[str]) -> int:
    """Execute the `create` subcommand."""
    ns = parse_or_code(make_parser("create", usage_summary=USAGE), args)
    if isinstance(ns, int):
        return ns
    return services.create_service.create()
