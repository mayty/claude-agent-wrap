# This file has been edited with the assistance of an AI tool.
"""General-purpose utility functions."""

import re
import stat
import uuid
from pathlib import Path


def sanitize_name(name: str) -> str:
    """Normalize a string to be a valid Docker image-name suffix."""
    lowered = name.lower()
    sanitized = re.sub(r"[^a-z0-9_.\-]", "-", lowered)
    return sanitized.strip("-")


def generate_uuid() -> str:
    """
    Generate a lowercase-hex UUID with dashes.

    Uses /proc/sys/kernel/random/uuid where available, falling back to uuid4.
    """
    proc_uuid = Path("/proc/sys/kernel/random/uuid")
    if proc_uuid.is_file():
        try:
            return proc_uuid.read_text().strip()
        except OSError:
            pass
    return str(uuid.uuid4())


def is_truthy_env(value: str) -> bool:
    """Check if an env var value is truthy (not empty/0/false/no)."""
    return value.lower() not in ("", "0", "false", "no")


def optional_truthy_env(value: str) -> bool | None:
    """
    Tri-state env flag: None when unset or empty, else the value's truthiness.

    The distinction :func:`is_truthy_env` collapses, and it matters for a flag that is on
    by default. An empty value counts as unset: exporting a variable with no value reads
    as clearing it, not as asking for the feature to be off.
    """
    if not value:
        return None
    return is_truthy_env(value)


def directory_size(path: Path) -> int:
    """
    Sum the apparent size of every regular file under *path*, recursively.

    Best-effort: a per-entry ``OSError`` yields a smaller total rather than aborting.
    Symlinks are not followed -- only their own size counts.
    """
    total = 0
    try:
        entries = list(path.rglob("*"))
    except OSError:
        return 0
    for entry in entries:
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode):
            total += st.st_size
    return total
