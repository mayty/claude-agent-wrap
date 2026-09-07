# This file has been edited with the assistance of an AI tool.
"""The `run` subcommand — launches Claude Code in a Docker container."""

import click

from agent_wrap.containers import core, services


@click.command(
    "run",
    # `add_help_option=False` is what makes `agent run --help` forward `--help` to Claude
    # Code rather than print the wrapper's own help; `ignore_unknown_options` is what lets
    # every other unrecognised flag through to `claude_args` instead of erroring.
    # `allow_interspersed_args` is deliberately left at its default: turning it off would
    # stop `-b` being recognised after an unknown flag, which argparse's parse_known_args
    # accepted and callers rely on.
    add_help_option=False,
    context_settings={"ignore_unknown_options": True},
)
@click.option(
    "-b",
    "--base",
    is_flag=True,
    help=(
        "Ignore any .claude-agent-wrap/Dockerfile in the current directory and launch the "
        "base 'claude-agent' image instead. The project's EXPOSE, agent-user, "
        "agent-run-args and agent-enable-startup directives are all skipped, so its "
        "startup script does not run either. Only the base image is built or rebuilt."
    ),
)
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
@click.pass_context
def run_command(ctx: click.Context, *, base: bool, claude_args: tuple[str, ...]) -> None:
    """Launch Claude Code in Docker"""
    # The grant a launch needs to register its project directory -- and the one that
    # performs the one-time import of a pre-SQLite projects.txt. `ctx.exit` raises, and
    # `enable_writes` clears the flag in a `finally`, so the exit path needs nothing.
    with core.projects_db.enable_writes():
        ctx.exit(services.launch_service.launch(use_base=base, claude_args=list(claude_args)))
