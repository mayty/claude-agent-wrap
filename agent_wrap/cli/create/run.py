# This file has been edited with the assistance of an AI tool.
"""The `create` subcommand — scaffolds a project Dockerfile."""

from typing import TYPE_CHECKING

from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

USAGE = ""
SUMMARY = "Scaffold .claude-agent-wrap/Dockerfile"


def build_parser() -> argparse.ArgumentParser:
    return make_parser("create", usage_summary=USAGE)


def run(args: list[str]) -> int:
    """Execute the `create` subcommand."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns
    return services.create_service.create()
