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

# The shared sidecar writes every project's logs under
# ``<tool_dir>/litellm-logs/<project_hash>/<provider>/<session>/`` and points each
# project's ``.claude/litellm-logs`` symlink at its own ``<hash>`` subtree.
CENTRAL_LOGS_DIRNAME = "litellm-logs"


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
    ``(group_root, display_name, is_transient)``:

    * With a marker — ``group_root`` is the marker's directory and
      ``is_transient`` is True (the group is an aggregated transient project,
      flagged with ``*`` in the UI). ``display_name`` is the marker's first
      non-empty line when present, otherwise the marker directory's name.
    * Without a marker anywhere up the tree — ``group_root`` is ``path`` itself,
      ``display_name`` is ``path.name``, and ``is_transient`` is False (each
      project stays on its own, matching the pre-grouping behaviour).

    ``is_transient`` reflects *marker presence*, not whether the name was
    customized: an empty marker still produces a transient group (named after its
    directory), and that group must be flagged just like a custom-named one.
    """
    # `path` first (a project dir can itself hold the marker), then ancestors.
    for candidate in (path, *path.parents):
        marker = candidate / MARKER_NAME
        if marker.is_file():
            name = _read_marker_name(marker)
            return candidate, name if name is not None else candidate.name, True
    return path, path.name, False


def orphaned_log_dirs(tool_dir: Path, projects: list[Path]) -> list[Path]:
    """
    Central ``<hash>`` log dirs not reachable from a registered, existing project.

    The sidecar writes logs under ``<tool_dir>/litellm-logs/<hash>/`` and each
    project's ``.claude/litellm-logs`` symlink resolves to its own ``<hash>`` dir.
    Any central child whose resolved path matches a registered project's resolved
    logs dir is reachable and excluded; the rest are orphaned — left behind by a
    deleted project, a stale ``projects.txt`` entry, or the sidecar's default-hash
    fallback bucket. Returned sorted for stable ordering.

    Best-effort: filesystem errors are swallowed so a single bad entry can never
    break stats or the viewer.
    """
    reachable: set[Path] = set()
    for project in projects:
        link = project / ".claude" / CENTRAL_LOGS_DIRNAME
        try:
            if link.is_dir():
                reachable.add(link.resolve())
        except OSError:
            continue

    central = tool_dir / CENTRAL_LOGS_DIRNAME
    orphaned: list[Path] = []
    try:
        children = list(central.iterdir())
    except OSError:
        return []
    for child in children:
        try:
            if not child.is_dir():
                continue
            if child.resolve() not in reachable:
                orphaned.append(child)
        except OSError:
            continue
    return sorted(orphaned)
