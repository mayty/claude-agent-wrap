# This file has been created with the assistance of an AI tool.
"""Constants for the CLI layer."""

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from agent_wrap.cli.models import CompleteFunc, RunFunc

#: Every registered subcommand, mapping its verb to (run, complete).
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
