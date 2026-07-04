# This file has been created with the assistance of an AI tool.
"""
The `logs` subcommand — a local web viewer for the LiteLLM request logs.

Everything is Python stdlib only (``http.server``) — no extra dependency, no
``agent rebuild``, no Docker. It runs on the host exactly like `agent stats`.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from agent_wrap.constants import (
    AGENT_LAUNCHES_DIR,  # noqa: F401  # referenced via globals() in run()
    LOGS_DEFAULT_PORT,
    LOGS_MAX_PORT,
    LOGS_MIN_PORT,
    LOGS_TOOL_DIR_ENV,
)
from agent_wrap.containers import services
from agent_wrap.lib.argparsing import make_parser, parse_or_code

USAGE = "[--port N] [--stop]"
SUMMARY = "Browse LiteLLM request logs in a local web viewer"

_USAGE_TEXT = (
    "Usage: agent logs [--port N] [--stop]\n\n"
    "Starts a local web viewer for the LiteLLM request logs written under each\n"
    "project's .claude/litellm-logs/ directory. Pick a project, then a session,\n"
    "and read every logged request chat-style.\n\n"
    "The viewer runs in the background and prints its connect line; if one is\n"
    "already running, the existing connect line is reprinted (the port is\n"
    "ignored).\n\n"
    "--port N binds the viewer to port N (default 8765); if busy, the next free\n"
    "port is used. The server binds to 127.0.0.1 only and is read-only.\n"
    "--stop stops the background viewer."
)


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
    parser = make_parser("logs", usage_summary=USAGE, description=_USAGE_TEXT)
    parser.add_argument("--port", type=_port, default=LOGS_DEFAULT_PORT, metavar="N")
    parser.add_argument("--stop", action="store_true", help="stop the background viewer")
    # Hidden internal flag: the re-exec'd child that actually runs the blocking
    # server. Suppressed so it stays out of help/USAGE and bashrc completion.
    parser.add_argument("--foreground", action="store_true", help=argparse.SUPPRESS)
    return parser


def run(args: list[str]) -> int:
    """Execute the `logs` subcommand."""
    # A detached child is pinned to its launching parent's tool_dir so both
    # sides resolve the same state file (see LOGS_TOOL_DIR_ENV / spawn_background).
    env_dir = os.environ.get(LOGS_TOOL_DIR_ENV)
    if env_dir:
        tool_dir = Path(env_dir)
        # Patch the module-level constant so state functions use the test's tmp_path.
        globals()["AGENT_LAUNCHES_DIR"] = tool_dir / ".agent-launches"

    ns = parse_or_code(build_parser(), args)
    if isinstance(ns, int):
        return ns

    if ns.stop:
        if ns.foreground or ns.port != LOGS_DEFAULT_PORT:
            print("usage: agent logs --stop (takes no other arguments)", file=sys.stderr)
            return 1
        return services.logs_service.stop_daemon()

    if ns.foreground:
        return services.logs_service.serve_foreground(ns.port)

    running = services.logs_service.running_server()
    if running is not None:
        print(services.logs_service.connect_line(running["port"]))
        return 0

    return services.logs_service.spawn_background(ns.port)
