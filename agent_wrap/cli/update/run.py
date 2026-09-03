# This file has been edited with the assistance of an AI tool.
"""The `update` subcommand — git-based self-update."""

import click

from agent_wrap.containers import services


@click.command("update")
@click.pass_context
def update_command(ctx: click.Context) -> None:
    """
    Pull upstream updates

    Fast-forward the wrapper checkout: on master only when a newer tag has been
    published, to that tag's commit; on any other branch to the branch tip, on any
    upstream commit. A changed default-CLAUDE.md replaces the user's copy while that copy
    is unmodified, and is left untouched with merge instructions once it was customized.

    Refused outright while any agent or sidecar container is still running, exiting 1
    after listing what it found -- an update rewrites the checkout every live agent's
    host-side code runs from, and re-provisions the interpreter underneath it. There is no
    override flag; stopping those containers is the only way past it. The logs viewer is
    stopped before the fast-forward and is not restarted here, so the next `agent run`
    starts it again.
    """
    ctx.exit(services.update_service.apply())
