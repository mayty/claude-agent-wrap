# This file has been edited with the assistance of an AI tool.
"""
agent-wrap CLI entry point.

Dispatches subcommands to the agent_wrap.cli package.
Called from agent-wrap.bashrc as: python3 -m agent_wrap <subcommand> [args...]
"""

from __future__ import annotations

import sys

from agent_wrap.cli import main as cli_main

if __name__ == "__main__":
    sys.exit(cli_main())
