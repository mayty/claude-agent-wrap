# This file has been edited with the assistance of an AI tool.
"""
The `logs` subcommand — a local web viewer for the LiteLLM request logs.

The HTTP layer is stdlib (``http.server``); change detection uses ``watchdog``, so the
viewer refreshes on filesystem events rather than on a timer. Still no ``agent
rebuild`` and no Docker — it runs on the host exactly like `agent stats`.
"""

import click

from agent_wrap.constants import (
    LOGS_DEFAULT_PORT,
    LOGS_MAX_PORT,
    LOGS_MIN_PORT,
)
from agent_wrap.containers import core, services


@click.command("logs")
@click.option(
    "-p",
    "--port",
    type=click.IntRange(LOGS_MIN_PORT, LOGS_MAX_PORT),
    metavar="N",
    help=(
        f"Bind the viewer to port N (default {LOGS_DEFAULT_PORT}); if busy, the next free "
        "port is used. It binds to 127.0.0.1 only and is read-only."
    ),
)
@click.option("-s", "--stop", is_flag=True, help="Stop the background viewer.")
# Hidden internal flag: the re-exec'd child that actually runs the blocking server.
# Hidden so it stays out of help, out of the synopsis, and out of shell completion.
@click.option("--foreground", is_flag=True, hidden=True)
@click.pass_context
def logs_command(ctx: click.Context, *, port: int | None, stop: bool, foreground: bool) -> None:
    """
    Browse LiteLLM request logs in a local web viewer

    Start a local web viewer for the request logs written under each project's
    .claude/litellm-logs/ directory. Pick a project, then a session, and read every
    logged request chat-style.

    The viewer runs in the background and prints its connect line; if one is already
    running, the existing connect line is reprinted (the port is ignored).
    """
    # `port` is left defaulting to None rather than to LOGS_DEFAULT_PORT so that
    # "was --port given?" is a real question below; comparing against the default
    # cannot distinguish an omitted flag from `--port 8765` typed out in full.
    if stop:
        if foreground or port is not None:
            services.display_service.error("agent logs --stop (takes no other arguments)")
            ctx.exit(1)
        ctx.exit(services.logs_service.stop_daemon())

    resolved_port = LOGS_DEFAULT_PORT if port is None else port

    if foreground:
        # The one grant this verb takes, and only on the child that actually serves. The
        # viewer is read-only about everything it *shows*, but it is the process that
        # keeps the request index current -- every reconcile ingests whatever the
        # sidecars have appended, and without the grant those writes are refused and
        # every consumer's totals freeze at the last `agent reindex`. The registry stays
        # ungranted: the daemon reads it on every tick and must not be able to alter it.
        with core.logs_db.enable_writes():
            ctx.exit(services.logs_service.serve_foreground(resolved_port))

    running = services.logs_service.running_server()
    if running is not None:
        # A viewer that has been claimed but is not listening yet is reported as such
        # rather than being started again -- the port in its claim is still provisional.
        line = (
            services.logs_service.starting_line(running["port"])
            if running["starting"]
            else services.logs_service.connect_line(running["port"])
        )
        services.display_service.info(line)
        ctx.exit(0)

    ctx.exit(services.logs_service.spawn_background(resolved_port))
