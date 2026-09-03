# This file has been created with the assistance of an AI tool.
"""
Which filesystem a path lives on.

Answers the one question ``os.stat`` cannot: not *which device*, but *what kind* --
ext4, drvfs, nfs. Callers use it to decide whether a filesystem supports something,
which a device number cannot tell them.

Linux only, by construction: the answer comes from ``/proc/self/mounts``. Everywhere
else it is None, which callers must read as "unknown" rather than "no" -- there is no
portable spelling of this, and guessing would be worse than declining to answer.
"""

from pathlib import Path

#: Where the kernel publishes the current mount table.
MOUNTS_FILE = Path("/proc/self/mounts")


def filesystem_type(path: Path) -> str | None:
    """
    Return the filesystem type *path* sits on, or None when it cannot be determined.

    The answer is the *longest* matching mount point, because mounts nest: ``/`` matches
    every path, so a shorter match is only ever the wrong answer for a path that also
    has a longer one.
    """
    try:
        table = MOUNTS_FILE.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        target = path.resolve()
    except OSError:
        target = path

    best_depth = -1
    best_type: str | None = None
    for line in table.splitlines():
        fields = line.split()
        # device, mount point, type, options, ... -- anything shorter is not a mount line.
        if len(fields) < 3:  # noqa: PLR2004
            continue
        # The kernel escapes spaces, tabs, newlines and backslashes in the mount point
        # as octal, so an unescaped compare would miss any path containing one.
        mount_point = Path(
            fields[1]
            .replace("\\040", " ")
            .replace("\\011", "\t")
            .replace("\\012", "\n")
            .replace("\\134", "\\")
        )
        if target != mount_point and not target.is_relative_to(mount_point):
            continue
        depth = len(mount_point.parts)
        if depth > best_depth:
            best_depth = depth
            best_type = fields[2]
    return best_type
