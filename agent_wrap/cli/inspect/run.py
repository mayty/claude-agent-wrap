# This file has been edited with the assistance of an AI tool.
"""The `inspect` subcommand — a read-only report of agent-wrap's state on this host."""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING

from agent_wrap.cli.inspect.constants import INSPECT_LABEL, USAGE_TEXT
from agent_wrap.cli.inspect.render import render
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

if TYPE_CHECKING:
    import argparse

    from agent_wrap.domain.status.models import InspectReport

USAGE = "[-j|--json] [-l|--lite]"
SUMMARY = "Show running sidecars, agents, and the rest of the current state"


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("inspect", usage_summary=USAGE, description=USAGE_TEXT)
    parser.add_argument(
        "-j",
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit the report as a single JSON document instead of tables.",
    )
    parser.add_argument(
        "-l",
        "--lite",
        action="store_true",
        help="Skip the npm-registry version check and the logs-size walk.",
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
        captured.append(services.inspect_service.build_report(lite=ns.lite))
    else:
        dsp.spin_while(
            label=INSPECT_LABEL,
            message="collecting…",
            done_message=lambda: None,
            work=lambda: captured.append(services.inspect_service.build_report(lite=ns.lite)),
        )
    report = captured[0]

    if ns.as_json:
        # asdict is safe because every model in the report is a frozen dataclass of
        # scalars — see domain/status/models.py, which exists to guarantee exactly this.
        dsp.info(json.dumps(dataclasses.asdict(report), indent=2))
    else:
        for line in render(report, dsp):
            dsp.info(line)
        # Warnings go through display.warning rather than riding the report's line list:
        # they belong on stderr, so a redirected report stays machine-readable and the
        # severity survives being piped.
        for text in report.warnings:
            dsp.warning(text)

    if not report.docker.available:
        # The report above still printed everything that does not need Docker; the
        # non-zero exit is what makes the degradation detectable by a script.
        return 1
    return 0
