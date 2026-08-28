# This file has been created with the assistance of an AI tool.
"""Constants for the launch domain subpackage."""

#: Expected number of agents queued behind the shared sidecar lock.
EXPECTED_QUEUE_DEPTH = 128

# Per-project state directories mounted into the container.
STATE_MOUNTS = {
    "sessions": "projects/-workspace",
    "memory": "projects/-workspace/memory",
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

#: Directory under the project's ``.claude/`` holding one subtree per container,
#: keyed by instance id.
INSTANCE_DIR_NAME = "instances"

# Per-container state. Claude Code keys these by PID and elects a singleton daemon
# through daemon.lock, but PIDs are namespace-local -- every container's claude runs
# as PID 1 -- so a project-wide mount makes concurrent agents in one directory read
# each other's PIDs as their own and displace each other's daemon. Keyed by instance
# id and removed when the container exits.
INSTANCE_STATE_MOUNTS = {
    "daemon": "daemon",
    "session-state": "sessions",
}

# Same reasoning as INSTANCE_STATE_MOUNTS. These also cannot be shared for a second,
# independent reason: Claude Code replaces all three via rename() (and rotates
# daemon.log the same way), which fails with EBUSY on a single-file bind mount.
INSTANCE_STATE_FILES = (
    "daemon.lock",
    "daemon.log",
    "daemon.status.json",
)

# How long an instance directory whose container is not running is left alone before
# the sweep collects it. A launching agent creates its directory before `docker run`
# starts the container, so for that window it is indistinguishable from a crashed
# one by liveness alone -- the grace period is what tells them apart.
INSTANCE_SWEEP_GRACE_SECONDS = 3600

#: Opt-out for starting the `agent logs` viewer on launch. Absent or empty means unset,
#: which is on -- exporting the var with no value reads as clearing it, not as asking for
#: the feature to be off.
AUTOSTART_LOGS_ENV = "AGENT_AUTOSTART_LOGS"

#: Claude Code flags marking a non-interactive invocation — the Telegram sidecar and the
#: self-update prompt are both skipped under these.
HEADLESS_FLAGS = frozenset({"-p", "--print", "--bare", "--safe-mode"})
