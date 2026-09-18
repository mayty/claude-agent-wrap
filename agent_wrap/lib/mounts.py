# This file has been created with the assistance of an AI tool.
"""
Which filesystem a path lives on.

Answers the one question ``os.stat`` cannot: not *which device*, but *what kind* --
ext4, drvfs, nfs. Callers use it to decide whether a filesystem supports something,
which a device number cannot tell them.

The answer comes from the OS mount table, wherever psutil can read one -- Linux, macOS,
the BSDs. Where it cannot, the answer is None, which callers must read as "unknown"
rather than "no": guessing would be worse than declining to answer.
"""

from pathlib import Path

import psutil


def filesystem_type(path: Path) -> str | None:
    """
    Return the filesystem type *path* sits on, or None when it cannot be determined.

    ``all=True`` is mandatory: the default keeps only physical devices, which drops
    exactly the ``drvfs`` / ``9p`` / ``nfs`` mounts every caller here exists to detect.

    The answer is the *longest* matching mount point, because mounts nest: ``/`` matches
    every path, so a shorter match is only ever the wrong answer for a path that also
    has a longer one.
    """
    try:
        partitions = psutil.disk_partitions(all=True)
    except OSError:
        return None

    try:
        target = path.resolve()
    except OSError:
        target = path

    best_depth = -1
    best_type: str | None = None
    for partition in partitions:
        mount_point = Path(partition.mountpoint)
        if target != mount_point and not target.is_relative_to(mount_point):
            continue
        depth = len(mount_point.parts)
        if depth > best_depth:
            best_depth = depth
            best_type = partition.fstype
    return best_type
