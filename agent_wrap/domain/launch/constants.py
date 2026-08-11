# This file has been created with the assistance of an AI tool.
"""Constants for the launch domain subpackage."""

#: Expected number of agents queued behind the shared sidecar lock.
EXPECTED_QUEUE_DEPTH = 128

# Per-project state directories mounted into the container.
STATE_MOUNTS = {
    "sessions": "projects/-workspace",
    "memory": "projects/-workspace/memory",
    "session-state": "sessions",
    "daemon": "daemon",
    "jobs": "jobs",
    "plans": "plans",
    "todos": "todos",
    "tasks": "tasks",
    "shell-snapshots": "shell-snapshots",
    "session-env": "session-env",
    "file-history": "file-history",
    "paste-cache": "paste-cache",
    "image-cache": "image-cache",
}

# Per-project state directories mounted outside the Claude home directory.
# Values are container-side path templates: ``{uid}`` is the container's effective
# UID, ``{home}`` the Claude home directory.
#
# ``claude-tmp`` carries the whole per-UID session temp tree. Its ``scratchpad/``
# and ``tasks/`` subdirectories sit under a session UUID minted at runtime, so they
# cannot be bind-mounted separately — the tree is mounted as one unit.
EXTERNAL_STATE_MOUNTS = {
    # The /tmp location is not our choice: Claude Code derives this path itself, so
    # the mount has to name it exactly. It is a container path, never a host one.
    "claude-tmp": "/tmp/claude-{uid}",  # noqa: S108
    "mcp-logs": "{home}/.cache/claude-cli-nodejs/-workspace",
}

#: Claude Code flags marking a non-interactive invocation — the Telegram sidecar and the
#: self-update prompt are both skipped under these.
HEADLESS_FLAGS = frozenset({"-p", "--print", "--bare", "--safe-mode"})
