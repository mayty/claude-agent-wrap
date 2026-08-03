# This file has been created with the assistance of an AI tool.
"""Constants for the logs CLI command."""

#: Long-form help printed for `agent logs -h`.
USAGE_TEXT = (
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
