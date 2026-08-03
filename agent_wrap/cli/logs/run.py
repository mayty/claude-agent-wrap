# This file has been created with the assistance of an AI tool.
"""
The `logs` subcommand — a local web viewer for the LiteLLM request logs.

Everything is Python stdlib only (``http.server``) — no extra dependency, no
``agent rebuild``, no Docker. It runs on the host exactly like `agent stats`.
"""

from __future__ import annotations

import argparse

from agent_wrap.cli.logs.constants import USAGE_TEXT
from agent_wrap.constants import (
    LOGS_DEFAULT_PORT,
    LOGS_MAX_PORT,
    LOGS_MIN_PORT,
)
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

USAGE = "[--port N] [--stop]"
SUMMARY = "Browse LiteLLM request logs in a local web viewer"


def _port(value: str) -> int:
    """Argparse ``type`` for ``--port``: an integer within the valid range."""
    try:
        port = int(value)
    except ValueError:
        msg = f"expects an integer, got '{value}'"
        raise argparse.ArgumentTypeError(msg) from None
    if not (LOGS_MIN_PORT <= port <= LOGS_MAX_PORT):
        msg = f"must be between {LOGS_MIN_PORT} and {LOGS_MAX_PORT}"
        raise argparse.ArgumentTypeError(msg)
    return port


def build_parser() -> argparse.ArgumentParser:
    parser = make_parser("logs", usage_summary=USAGE, description=USAGE_TEXT)
    parser.add_argument("--port", type=_port, default=LOGS_DEFAULT_PORT, metavar="N")
    parser.add_argument("--stop", action="store_true", help="stop the background viewer")
    # Hidden internal flag: the re-exec'd child that actually runs the blocking
    # server. Suppressed so it stays out of help/USAGE and bashrc completion.
    parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    return parser


def run(args: list[str]) -> int:
    """Execute the `logs` subcommand."""
    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns

    if ns.stop:
        if ns.foreground or ns.port != LOGS_DEFAULT_PORT:
            services.display_service.error("usage: agent logs --stop (takes no other arguments)")
            return 1
        return services.logs_service.stop_daemon()

    if ns.foreground:
        return services.logs_service.serve_foreground(ns.port)

    running = services.logs_service.running_server()
    if running is not None:
        services.display_service.info(services.logs_service.connect_line(running["port"]))
        return 0

    return services.logs_service.spawn_background(ns.port)
