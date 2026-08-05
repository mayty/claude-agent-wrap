# This file has been edited with the assistance of an AI tool.
"""Command metadata read reflectively off each subcommand's ``run`` module."""

from __future__ import annotations

from importlib import import_module

from agent_wrap.cli.constants import COMMANDS
from agent_wrap.cli.models import Command


def command_meta() -> dict[str, Command]:
    """Return metadata for every registered command keyed by name."""
    meta: dict[str, Command] = {}
    for name in COMMANDS:
        mod = import_module(f"agent_wrap.cli.{name}.run")
        meta[name] = Command(
            name=name,
            usage=getattr(mod, "USAGE", ""),
            summary=getattr(mod, "SUMMARY", ""),
        )
    return meta


def format_usage(commands: dict[str, Command]) -> str:
    """Render the help block from registered commands."""
    name_width = max((len(c.name) for c in commands.values()), default=0)
    usage_width = max((len(c.usage) for c in commands.values()), default=0)
    rows = [
        f"  {c.name:<{name_width}}  {c.usage:<{usage_width}}  {c.summary}".rstrip()
        for c in commands.values()
    ]
    return "\n".join(["Usage: agent <command> [args...]", "", "Commands:", *rows]) + "\n"
