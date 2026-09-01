# This file has been edited with the assistance of an AI tool.
"""Constants for the updates domain subpackage."""

from enum import Enum

RESOURCE_FILES = {
    "agent-wrap.bashrc",
}

# A change to either of these means the interpreter the wrapper should be running on
# has moved. Unlike the two sets above, this one is not merely advisory: a stale pin
# leaves users on an interpreter that no longer receives CPython or OpenSSL patches,
# so the update re-runs the bootstrap itself.
BOOTSTRAP_FILES = {
    "python-pin.env",
    "bin/agent-bootstrap",
}


class MdState(Enum):
    MATCHES = "matches"
    CUSTOMIZED = "customized"
    MISSING = "missing"


class MdPropagation(Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    CONFLICT = "conflict"
