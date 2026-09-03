# This file has been edited with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

import click

from agent_wrap.containers import services


@click.command("update")
@click.pass_context
def update_command(ctx: click.Context) -> None:
    """Pull upstream updates"""
    ctx.exit(services.update_service.apply())
