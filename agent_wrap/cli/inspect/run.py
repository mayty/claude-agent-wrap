# This file has been created with the assistance of an AI tool.
"""The `inspect` subcommand — a read-only report of agent-wrap's state on this host."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from agent_wrap.cli.inspect.constants import INSPECT_LABEL
from agent_wrap.cli.inspect.render import render
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

    from agent_wrap.domain.status.models import InspectReport

USAGE = "[--json]"
SUMMARY = "Show running sidecars, agents, and the rest of the current state"

_USAGE_TEXT = (
    "Usage: agent inspect [--json]\n\n"
    "Reports what agent-wrap is currently doing on this host: the sidecar\n"
    "containers that are up (with their image, port, health, uptime, and how\n"
    "many agents are attached), the agent containers running against them (with\n"
    "their image, project directory, and provider), the logs viewer, the\n"
    "on-disk log footprint, per-provider secret readiness, the installed\n"
    "wrapper revision, and the host facts behind most launch surprises.\n\n"
    "Read-only: it starts nothing, stops nothing, and writes nothing. It also\n"
    "makes no network call, so the wrapper revision it reports is the local\n"
    "one — use `agent update` to check for a newer release.\n\n"
    "--json emits the same report as one JSON document instead of tables.\n\n"
    "Exits 1 when the Docker daemon cannot be reached; every section that does\n"
    "not depend on Docker is still reported."
)


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("inspect", usage_summary=USAGE, description=_USAGE_TEXT)
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the report as a single JSON document instead of tables.",
    )
    return parser


def run(args: list[str]) -> int:
    """Execute the `inspect` subcommand."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns

    dsp = services.display_service

    captured: list[InspectReport] = []
    if ns.as_json:
        # No spinner: its animation goes to stdout, which would corrupt the document.
        captured.append(services.inspect_service.build_report())
    else:
        dsp.spin_while(
            label=INSPECT_LABEL,
            message="collecting…",
            done_message=lambda: None,
            work=lambda: captured.append(services.inspect_service.build_report()),
        )
    report = captured[0]

    if ns.as_json:
        # asdict is safe because every model in the report is a frozen dataclass of
        # scalars — see domain/status/models.py, which exists to guarantee exactly this.
        dsp.info(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        for line in render(report, dsp):
            dsp.info(line)

    if not report.docker.available:
        # The report above still printed everything that does not need Docker; the
        # non-zero exit is what makes the degradation detectable by a script.
        return 1
    return 0
