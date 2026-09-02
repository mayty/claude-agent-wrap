# This file has been edited with the assistance of an AI tool.
"""Constants for the updates domain subpackage."""

from enum import Enum

RESOURCE_FILES = {
    "agent-wrap.bashrc",
}

# A change to any of these means the environment the wrapper should be running on has
# moved -- the interpreter pin, the provisioning script itself, or the locked set of
# dependencies. Unlike the two sets above, this one is not merely advisory: a stale pin
# leaves users on an interpreter that no longer receives CPython or OpenSSL patches, and
# stale constraints leave newly merged code importing packages that are not installed,
# so the update re-runs the bootstrap itself.
BOOTSTRAP_FILES = {
    "python-pin.env",
    "bin/agent-bootstrap",
    "bin/requirements.txt",
}


class MdState(Enum):
    MATCHES = "matches"
    CUSTOMIZED = "customized"
    MISSING = "missing"


class MdPropagation(Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    CONFLICT = "conflict"
