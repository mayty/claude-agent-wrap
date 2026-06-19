# This file has been created with the assistance of an AI tool.
"""
Atomic file-write helpers.

Write to a sibling temp file then ``os.replace`` it over the destination, so a
reader never observes a half-written file and concurrent writers can't interleave
partial content. Consolidates the tmp-then-replace idiom that was reimplemented
across config, the logs viewer, and the provider key/pricing caches.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """
    Atomically write *text* to *path*.

    Writes to ``<path>.tmp`` in the same directory (so the replace is a rename on
    the same filesystem, not a cross-device copy) then renames it over the
    destination via ``Path.replace`` (atomic on POSIX). The parent directory is
    created if missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def atomic_write_json(path: Path, data: object) -> None:
    """Atomically write *data* as indented JSON (trailing newline) to *path*."""
    atomic_write_text(path, json.dumps(data, indent=2) + "\n")
