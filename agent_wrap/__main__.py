# This file has been edited with the assistance of an AI tool.
"""
agent-wrap CLI entry point: ``python3 -m agent_wrap <verb> [args...]``.

``prog_name`` is passed explicitly because this module is reached through ``bin/agent``,
which execs ``-m agent_wrap``: click would otherwise detect the program name as
``python -m agent_wrap`` and print that in every usage line. It also derives the
shell-completion environment variable (``_AGENT_COMPLETE``) from the program name.

The group carries the same name so that a test harness, which takes its program name
from the command rather than from ``sys.argv``, renders the usage lines identically.
"""

import click

from agent_wrap.cli import command_groups
from agent_wrap.constants import CLI_CONTEXT_SETTINGS


@click.group("agent", context_settings=CLI_CONTEXT_SETTINGS)
def cli_root() -> None: ...


for command_group in command_groups:
    cli_root.add_command(command_group)


if __name__ == "__main__":
    cli_root(prog_name="agent")
