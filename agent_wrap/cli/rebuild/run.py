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
    """
    Rebuild Docker image

    Rebuild the resolved image for the current directory, passing the HOST_UID/HOST_GID
    build args. A project image is rebuilt with --no-cache; the base image reuses docker's
    layer cache below the Claude Code CLI install, which is reinstalled either way.

    This is the force. `agent run` already builds whatever is missing or stale by itself,
    so what is left for this verb is the rebuild the wrapper cannot infer -- most often
    applying an edit just made to the project's own Dockerfile, which nothing hashes. The
    base image is still ensured underneath, so a project build never runs on an absent or
    stale base.
    """
    ctx.exit(services.build_service.rebuild(full=full))
