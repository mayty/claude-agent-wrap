# This file has been edited with the assistance of an AI tool.
#
# agent-wrap: Docker-based wrapper for running Claude Code CLI.
#
# Thin dispatcher — all logic lives in main.py / agent_wrap/. These functions
# just forward to the Python implementation, keeping the shell integration
# (source this file, get the functions) for backward compatibility.

_agent_wrap_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

agent() {
    python3 "$_agent_wrap_dir/main.py" agent "$@"
}

rebuild_agent() {
    python3 "$_agent_wrap_dir/main.py" rebuild "$@"
}

create_custom_agent() {
    python3 "$_agent_wrap_dir/main.py" create "$@"
}

agent_usage() {
    python3 "$_agent_wrap_dir/main.py" usage "$@"
}

agent-wrap_update() {
    python3 "$_agent_wrap_dir/main.py" update "$@"
}
