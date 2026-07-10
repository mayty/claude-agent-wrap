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

#: Claude Code flags marking a non-interactive invocation — the Telegram sidecar and the
#: self-update prompt are both skipped under these.
HEADLESS_FLAGS = frozenset({"-p", "--print", "--bare", "--safe-mode"})
