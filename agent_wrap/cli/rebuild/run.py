# This file has been edited with the assistance of an AI tool.
"""The `rebuild` subcommand — builds Docker images."""

import click

from agent_wrap.containers import services


@click.command("rebuild")
@click.option(
    "-f",
    "--full",
    is_flag=True,
    help="Rebuild the base 'claude-agent' image first, then the project image.",
)
@click.pass_context
def rebuild_command(ctx: click.Context, *, full: bool) -> None:
    """Rebuild Docker image"""
    ctx.exit(services.build_service.rebuild(full=full))
