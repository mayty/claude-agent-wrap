# This file has been edited with the assistance of an AI tool.
"""
Atomic file-write helpers.

Write to a sibling temp file then ``os.replace`` it over the destination, so a
reader never observes a half-written file. Consolidates the tmp-then-replace idiom
that was reimplemented across config, the logs viewer, and the provider key/pricing
caches.

The temp file gets a *unique* name (via ``tempfile.mkstemp``) so that concurrent
writers to the same destination never contend for one shared temp path — a fixed
``<path>.tmp`` name let two processes race on ``replace``, where the second one
found the temp already consumed and raised ``FileNotFoundError``. With unique temp
names each ``replace`` is independent; concurrent writers are last-writer-wins on
*content*, which is fine for the idempotent payloads these helpers carry (settings
hooks, statusline, project registry).
"""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """
    Atomically write *text* to *path*.

    Writes to a unique temp file in the same directory (so the replace is a rename
    on the same filesystem, not a cross-device copy) then renames it over the
    destination via ``Path.replace`` (atomic on POSIX). The parent directory is
    created if missing. The temp file is removed if the write or replace fails, so
    no stray ``*.tmp`` files are left behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, data: object) -> None:
    """Atomically write *data* as indented JSON (trailing newline) to *path*."""
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
