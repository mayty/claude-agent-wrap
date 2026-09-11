# This file has been created with the assistance of an AI tool.
"""The agent-wrap CLI: one click command per verb, aggregated for the root group."""

from agent_wrap.cli.cleanup.run import cleanup_command
from agent_wrap.cli.create.run import create_command
from agent_wrap.cli.inspect.run import inspect_command
from agent_wrap.cli.logs.run import logs_command
from agent_wrap.cli.rebuild.run import rebuild_command
from agent_wrap.cli.reindex.run import reindex_command
from agent_wrap.cli.run.run import run_command
from agent_wrap.cli.secrets.run import secrets_group
from agent_wrap.cli.stats.run import stats_command
from agent_wrap.cli.update.run import update_command

#: Every registered subcommand, in the order the root group registers them. Click sorts
#: its own ``Commands:`` listing alphabetically, so this order is not what the reader
#: sees -- it is kept alphabetical only so the two agree.
command_groups = (
    cleanup_command,
    create_command,
    inspect_command,
    logs_command,
    rebuild_command,
    reindex_command,
    run_command,
    secrets_group,
    stats_command,
    update_command,
)
