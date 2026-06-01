# This file has been edited with the assistance of an AI tool.
#
# agent-wrap: Docker-based wrapper for running Claude Code CLI.
#
# Thin dispatcher — all logic lives in agent_wrap/ (via __main__.py). This
# function just forwards to the Python implementation.

_agent_wrap_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

agent() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap "$@"
}
