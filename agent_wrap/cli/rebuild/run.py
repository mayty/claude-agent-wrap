# This file has been created with the assistance of an AI tool.
"""The `rebuild` subcommand — builds Docker images."""

from typing import TYPE_CHECKING

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

USAGE = "[-f|--full]"
SUMMARY = "Rebuild Docker image"


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("rebuild", usage_summary=USAGE)
    parser.add_argument(
        "-f",
        "--full",
        action="store_true",
        help="Rebuild the base 'claude-agent' image first, then the project image.",
    )
    return parser


def run(args: list[str]) -> int:
    """Execute the `rebuild` subcommand."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns
    return services.build_service.rebuild(full=ns.full)
