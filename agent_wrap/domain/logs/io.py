# This file has been edited with the assistance of an AI tool.
"""
Where a project's logs live, and which projects share a group.

All that is left of what was once the viewer's whole read path. Every consumer reads
``logs.db`` now: the session lists and their change markers come from the ``sessions``
table (see :mod:`agent_wrap.domain.logs.listing`) and an open session's records come
from ``requests`` (see :mod:`agent_wrap.domain.logs.stream`). Nothing here opens a log
file — the two questions below are about the *shape* of the tree, which is a matter of
directories and the project registry rather than of records.
"""

from pathlib import Path
from typing import TYPE_CHECKING

from agent_wrap.constants import (
    LITELLM_LOGS_DIRNAME,
    ORPHANED_LABEL,
)

if TYPE_CHECKING:
    from agent_wrap.domain.logs.models import GroupInfo
    from agent_wrap.domain.stats.service import StatsService


def logs_dir(project: Path) -> Path:
    return project / ".claude" / LITELLM_LOGS_DIRNAME


def list_groups(stats_service: StatsService, projects: list[Path]) -> list[GroupInfo]:
    """
    Group registered projects into transient projects by ``.agent_stats_leaf``.

    Returns one dict per group, ordered deterministically by group-root path so
    that a group's index is a stable id across requests. Each entry carries:

    * ``root`` — the group root :class:`~pathlib.Path` (marker dir, or the
      project itself when unmarked),
    * ``name`` — the group root's directory name,
    * ``paths`` — every member project :class:`~pathlib.Path` in the group,
    * ``logs_dirs`` — the LiteLLM logs dirs to scan for the group.

    Projects without a ``.claude/litellm-logs`` directory are skipped, mirroring
    the pre-grouping behaviour. Members are kept in registry order. A synthetic
    ``<orphaned>`` group is appended last (when present) for central log dirs left
    behind by deleted projects / stale registry entries — its ``logs_dirs`` are the
    central ``<hash>`` dirs themselves and it has no member ``paths``.
    """
    if not projects:
        return []
    names: dict[Path, str] = {}
    members: dict[Path, list[Path]] = {}
    for path in projects:
        if not logs_dir(path).is_dir():
            continue
        root, name, _transient = stats_service.resolve_group(path)
        if root not in members:
            members[root] = []
            names[root] = name
        members[root].append(path)

    # Sort by group-root path so ids are stable; callers re-sort the *public*
    # list (by recency) without disturbing this id assignment.
    groups: list[GroupInfo] = [
        {
            "root": root,
            "name": names[root],
            "paths": members[root],
            "logs_dirs": [logs_dir(p) for p in members[root]],
        }
        for root in sorted(members)
    ]

    orphaned = stats_service.orphaned_log_dirs(projects)
    if orphaned:
        groups.append(
            {
                "root": Path(ORPHANED_LABEL),
                "name": ORPHANED_LABEL,
                "paths": [],
                "logs_dirs": orphaned,
            }
        )
    return groups
