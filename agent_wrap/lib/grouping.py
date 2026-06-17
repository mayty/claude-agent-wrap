# This file has been created with the assistance of an AI tool.
"""
Group physical project paths into transient "leaf" projects for display.

When many agents are launched programmatically, each runs in its own
subdirectory and is recorded as a separate project in ``projects.txt`` — which
turns into a lot of noise in both ``agent stats`` and the logs viewer. Dropping
a ``.agent_stats_leaf`` marker file somewhere above those directories collapses
every project beneath it into one aggregated entry.

The upward walk uses the **literal (symlinked) path as recorded**, never the
resolved one. This is deliberate: it lets you aggregate otherwise-unrelated
projects by creating a common directory that holds a ``.agent_stats_leaf`` plus
symlinks to each real project, then launching agents through those symlink
paths. Resolving the path would collapse each symlink to its real location and
defeat the feature, so we must walk it as-is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

MARKER_NAME = ".agent_stats_leaf"


def _read_marker_name(marker: Path) -> str | None:
    """Return the marker's first non-empty line, or None when empty/unreadable."""
    try:
        text = marker.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line:
            return line
    return None


def resolve_group(path: Path) -> tuple[Path, str, bool]:
    """
    Resolve the transient-project group a project path belongs to.

    Walks up from ``path`` (inclusive) along its **literal** components looking
    for the nearest ``.agent_stats_leaf``. Returns
    ``(group_root, display_name, is_custom)``:

    * With a marker — ``group_root`` is the marker's directory. If the marker's
      first non-empty line is non-empty, ``display_name`` is that line and
      ``is_custom`` is True; otherwise ``display_name`` falls back to the marker
      directory's name and ``is_custom`` is False.
    * Without a marker anywhere up the tree — ``group_root`` is ``path`` itself,
      ``display_name`` is ``path.name``, and ``is_custom`` is False (each project
      stays on its own, matching the pre-grouping behaviour).
    """
    # `path` first (a project dir can itself hold the marker), then ancestors.
    for candidate in (path, *path.parents):
        marker = candidate / MARKER_NAME
        if marker.is_file():
            custom = _read_marker_name(marker)
            if custom is not None:
                return candidate, custom, True
            return candidate, candidate.name, False
    return path, path.name, False
