# This file has been edited with the assistance of an AI tool.
"""The `create` subcommand — scaffolds a project Dockerfile."""

import click

from agent_wrap.containers import services


@click.command("create")
@click.pass_context
def create_command(ctx: click.Context) -> None:
    """Scaffold .claude-agent-wrap/Dockerfile"""
    ctx.exit(services.create_service.create())
