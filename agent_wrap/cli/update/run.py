# This file has been created with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

from typing import TYPE_CHECKING

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

USAGE = ""
SUMMARY = "Pull upstream updates"


def build_parser() -> argparse.ArgumentParser:
    return make_parser("update", usage_summary=USAGE)


def run(args: list[str]) -> int:
    """Execute the `update` subcommand."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns
    return services.update_service.apply()
