# This file has been edited with the assistance of an AI tool.
"""
Atomic file-write helpers.

Write to a sibling temp file then ``os.replace`` it over the destination, so a reader
never observes a half-written file.

The temp file gets a *unique* name, because a fixed ``<path>.tmp`` lets two processes
race on ``replace`` -- the second finds the temp already consumed and raises
``FileNotFoundError``. Unique names make each ``replace`` independent; concurrent
writers are then last-writer-wins on content, which suits the idempotent payloads these
helpers carry.
"""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_bytes(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """
    Atomically write *data* to *path*, optionally chmod'ing the temp file first.

    The temp file goes in the same directory, so the replace is a same-filesystem rename
    rather than a cross-device copy, and is removed if the write or replace fails. The
    *mode* is applied before the rename, so the destination is never briefly readable at
    ``mkstemp``'s 0600-but-then-inherited default.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if mode is not None:
            tmp.chmod(mode)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write *text* to *path*."""
    atomic_write_bytes(path, text.encode())


def atomic_write_json(path: Path, data: object) -> None:
    """Atomically write *data* as indented JSON (trailing newline) to *path*."""
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
