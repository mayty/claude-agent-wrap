# This file has been edited with the assistance of an AI tool.
"""The `create` subcommand — scaffolds a project Dockerfile."""

import click

from agent_wrap.containers import services


@click.command("create")
@click.pass_context
def create_command(ctx: click.Context) -> None:
    """
    Scaffold .claude-agent-wrap/Dockerfile

    Write a minimal project Dockerfile (FROM claude-agent) in the current directory,
    pre-populated with the `# agent-name: <sanitized-dirname>` directive every project
    image is required to carry. Add the project's own RUN steps to it, then apply them
    with `agent rebuild`.

    Never overwrites: an existing .claude-agent-wrap/Dockerfile is an error, and so is a
    leftover deprecated Dockerfile.agent, which it asks you to move instead.
    """
    ctx.exit(services.create_service.create())
