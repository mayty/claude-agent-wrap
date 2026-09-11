# This file has been created with the assistance of an AI tool.
"""Data models for the updates domain."""

from typing import NamedTuple


class GitFullResult(NamedTuple):
    stdout: str
    returncode: int
    stderr: str


class BehindCountResult(NamedTuple):
    branch: str
    commits_behind: int
    target_ref: str


class WrapperRevision(NamedTuple):
    """
    The installed wrapper's git identity, resolved locally.

    Every field degrades to "" rather than being absent, so a wrapper installed from a
    tarball (no git metadata at all) reports blanks instead of failing the caller.
    """

    #: Current branch, "detached" on a detached HEAD, or "" outside a git repo.
    branch: str
    #: Abbreviated commit sha, or "".
    commit: str
    #: ``git describe`` output — the newest reachable tag plus distance, or "".
    describe: str
    #: Whether the working tree has uncommitted changes.
    dirty: bool
