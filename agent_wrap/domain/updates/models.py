# This file has been created with the assistance of an AI tool.
"""Data models for the updates domain."""

from __future__ import annotations

from enum import Enum
from typing import NamedTuple


class MdState(Enum):
    MATCHES = "matches"
    CUSTOMIZED = "customized"
    MISSING = "missing"


class MdPropagation(Enum):
    UNCHANGED = "unchanged"
    UPDATED = "updated"
    CONFLICT = "conflict"


class GitFullResult(NamedTuple):
    """Full git command result including stderr."""

    stdout: str
    returncode: int
    stderr: str


class BehindCountResult(NamedTuple):
    """Result of checking how far behind origin a branch is."""

    branch: str
    commits_behind: int
    target_ref: str
