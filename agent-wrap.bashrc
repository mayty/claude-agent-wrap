# This file has been edited with the assistance of an AI tool.
#
# agent-wrap: Docker-based wrapper for running Claude Code CLI.
#
# Thin dispatcher — all logic lives in agent_wrap/ (via __main__.py). These functions
# just forward to the Python implementation, keeping the shell integration
# (source this file, get the functions) for backward compatibility.

_agent_wrap_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

agent() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap agent "$@"
}

rebuild_agent() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap rebuild "$@"
}

create_custom_agent() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap create "$@"
}

agent_usage() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap usage "$@"
}

agent-wrap_update() {
    PYTHONPATH="$_agent_wrap_dir" python3 -m agent_wrap update "$@"
}
