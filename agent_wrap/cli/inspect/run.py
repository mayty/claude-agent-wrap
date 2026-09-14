# This file has been edited with the assistance of an AI tool.
"""The `inspect` subcommand — a read-only report of agent-wrap's state on this host."""

import dataclasses
import json

import click

from agent_wrap.cli.inspect.constants import INSPECT_LABEL
from agent_wrap.cli.inspect.render import render
from agent_wrap.containers import services


@click.command("inspect")
@click.option(
    "-j",
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the report as a single JSON document instead of tables.",
)
@click.option(
    "-l",
    "--lite",
    is_flag=True,
    help=(
        "Skip the three slowest steps: the npm-registry version check, the logs-size walk, "
        "and the stale-image sweep. Everything else is reported as usual, including both "
        "installed Claude Code versions. Use it from a project startup script, which runs "
        "while holding the host-global startup lock."
    ),
)
@click.pass_context
def inspect_command(ctx: click.Context, *, as_json: bool, lite: bool) -> None:
    """
    Show running sidecars, agents, and the current wrapper state

    Report what agent-wrap is currently doing on this host: the sidecar containers that
    are up (with their image, port, health, uptime, and how many agents are attached),
    the agent containers running against them (with their image, project directory, and
    provider), the logs viewer, the on-disk log footprint, per-provider secret readiness,
    the installed wrapper revision, the Claude Code version in the base image and in this
    project's own image, and the host facts behind most launch surprises.

    It closes with every registered project whose own image is already stale, so the
    rebuilds coming across the whole fleet are stated before they are paid for -- or one
    green line when there are none. A project that declares no Dockerfile is not listed
    (the base image row above covers it), nor is one whose image was never built on this
    host.

    Read-only: it starts no agent, stops nothing, and writes nothing. It does start a
    throwaway container per image to read the Claude Code version installed there, and
    one of those queries the npm registry to report whether a newer version exists. The
    wrapper revision is always read locally -- use `agent update` to check for a newer
    release.

    Exits 1 when the Docker daemon cannot be reached; every section that does not depend
    on Docker is still reported.
    """
    dsp = services.display_service

    if as_json:
        # No spinner: its animation goes to stdout, which would corrupt the document.
        report = services.inspect_service.build_report(lite=lite)
    else:
        report = dsp.spin_while(
            label=INSPECT_LABEL,
            message="collecting…",
            work=lambda: services.inspect_service.build_report(lite=lite),
        )

    if as_json:
        # asdict is safe because every model in the report is a frozen dataclass of
        # scalars — see domain/status/models.py, which exists to guarantee exactly this.
        dsp.info(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        dsp.show(render(report, dsp))
        # Warnings go through display.warning rather than riding the report itself:
        # they belong on stderr, so a redirected report stays machine-readable and the
        # severity survives being piped.
        for text in report.warnings:
            dsp.warning(text)

    if not report.docker.available:
        # The report above still printed everything that does not need Docker; the
        # non-zero exit is what makes the degradation detectable by a script.
        ctx.exit(1)
