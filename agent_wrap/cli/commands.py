# This file has been edited with the assistance of an AI tool.
"""Static command registry — single source of truth for all CLI subcommands."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module

from agent_wrap.cli.cleanup.complete import complete as cleanup_complete
from agent_wrap.cli.cleanup.run import run as cleanup_run
from agent_wrap.cli.create.complete import complete as create_complete
from agent_wrap.cli.create.run import run as create_run
from agent_wrap.cli.inspect.complete import complete as inspect_complete
from agent_wrap.cli.inspect.run import run as inspect_run
from agent_wrap.cli.logs.complete import complete as logs_complete
from agent_wrap.cli.logs.run import run as logs_run
from agent_wrap.cli.rebuild.complete import complete as rebuild_complete
from agent_wrap.cli.rebuild.run import run as rebuild_run
from agent_wrap.cli.run.complete import complete as run_complete
from agent_wrap.cli.run.run import run as run_run
from agent_wrap.cli.secrets.complete import complete as secrets_complete
from agent_wrap.cli.secrets.run import run as secrets_run
from agent_wrap.cli.stats.complete import complete as stats_complete
from agent_wrap.cli.stats.run import run as stats_run
from agent_wrap.cli.update.complete import complete as update_complete
from agent_wrap.cli.update.run import run as update_run

RunFunc = Callable[[list[str]], int]
CompleteFunc = Callable[[int, list[str]], list[str]]

COMMANDS: dict[str, tuple[RunFunc, CompleteFunc]] = {
    "cleanup": (cleanup_run, cleanup_complete),
    "create": (create_run, create_complete),
    "inspect": (inspect_run, inspect_complete),
    "logs": (logs_run, logs_complete),
    "rebuild": (rebuild_run, rebuild_complete),
    "run": (run_run, run_complete),
    "secrets": (secrets_run, secrets_complete),
    "stats": (stats_run, stats_complete),
    "update": (update_run, update_complete),
}


@dataclass(frozen=True)
class _Command:
    name: str
    usage: str
    summary: str


def command_meta() -> dict[str, _Command]:
    """Return metadata for every registered command keyed by name."""
    meta: dict[str, _Command] = {}
    for name in COMMANDS:
        mod = import_module(f"agent_wrap.cli.{name}.run")
        meta[name] = _Command(
            name=name,
            usage=getattr(mod, "USAGE", ""),
            summary=getattr(mod, "SUMMARY", ""),
        )
    return meta


def format_usage(commands: dict[str, _Command]) -> str:
    """Render the help block from registered commands."""
    name_width = max((len(c.name) for c in commands.values()), default=0)
    usage_width = max((len(c.usage) for c in commands.values()), default=0)
    rows = [
        f"  {c.name:<{name_width}}  {c.usage:<{usage_width}}  {c.summary}".rstrip()
        for c in commands.values()
    ]
    return "\n".join(["Usage: agent <command> [args...]", "", "Commands:", *rows]) + "\n"
