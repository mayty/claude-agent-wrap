# This file has been edited with the assistance of an AI tool.
"""Static command registry — single source of truth for all CLI subcommands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_wrap.cli import create, logs, rebuild, run, secrets, stats, update

if TYPE_CHECKING:
    from collections.abc import Callable

COMMANDS: dict[str, Callable[[list[str]], int]] = {
    "create": create.run,
    "logs": logs.run,
    "rebuild": rebuild.run,
    "run": run.run,
    "secrets": secrets.run,
    "stats": stats.run,
    "update": update.run,
}
