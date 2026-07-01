# This file has been edited with the assistance of an AI tool.
"""The `rebuild` subcommand — builds Docker images."""

from __future__ import annotations

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

USAGE = "[--full]"
SUMMARY = "Rebuild Docker image"


def _build_parser():
    parser = make_parser("rebuild", usage_summary=USAGE)
    parser.add_argument(
        "--full",
        action="store_true",
        help="Rebuild the base 'claude-agent' image first, then the project image.",
    )
    return parser


def run(args: list[str]) -> int:
    """Execute the `rebuild` subcommand."""
    ns = parse_or_code(_build_parser(), args)
    if isinstance(ns, int):
        return ns
    return services.build_service.rebuild(full=ns.full)
