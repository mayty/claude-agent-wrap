# This file has been created with the assistance of an AI tool.
"""Data models for the projects database."""

from dataclasses import dataclass
from typing import NamedTuple


@dataclass(frozen=True)
class Project:
    """
    A directory ``agent run`` has been launched from.

    ``path`` is the path as the user sees it -- ``$PWD``-preserving, so a project reached
    through a symlink keeps the spelling the user typed rather than its resolved target.

    Timestamps are unix nanoseconds. They are integers rather than ISO text so that
    ``MAX(last_seen_at)`` can stand in for the mtime the logs viewer's ETag used to read
    off the registry file.
    """

    path: str
    first_seen_at: int
    last_seen_at: int


class Revision(NamedTuple):
    """
    A cheap summary of the projects table, for change detection.

    Both fields move whenever the table does: ``last_change_ns`` on any write (every
    launch refreshes ``last_seen_at``), ``count`` on an insert or delete. ``None`` for
    ``last_change_ns`` means the table is empty.
    """

    last_change_ns: int | None
    count: int
