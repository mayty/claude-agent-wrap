# This file has been edited with the assistance of an AI tool.
"""Constants for the updates domain subpackage."""

from enum import Enum

REBUILD_FILES = {
    "agent_wrap/__main__.py",
    "ops/Dockerfile",
}

RESOURCE_FILES = {
    "agent-wrap.bashrc",
}


class MdState(Enum):
    MATCHES = "matches"
    CUSTOMIZED = "customized"
    MISSING = "missing"


class MdPropagation(Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    CONFLICT = "conflict"
