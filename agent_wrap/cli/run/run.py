# This file has been created with the assistance of an AI tool.
"""The `run` subcommand — launches Claude Code in a Docker container."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser

if TYPE_CHECKING:
    import argparse

USAGE = "[--base] [claude-args...]"
SUMMARY = "Launch Claude Code in Docker"


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("run", usage_summary=USAGE, add_help=False)
    parser.add_argument("--base", action="store_true")
    return parser


def run(args: list[str]) -> int:
    """Execute the `run` subcommand. Forwards exit code from docker run."""
    parser = build_parser()
    ns, claude_args = parser.parse_known_args(args)
    return services.launch_service.launch(use_base=ns.base, claude_args=claude_args)
