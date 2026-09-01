# This file has been created with the assistance of an AI tool.
"""Stable path hashing."""

import hashlib
from pathlib import Path


def project_path_hash(path: Path) -> str:
    """
    Return a stable 16-hex-char SHA-256 hash of *path*'s fully-resolved absolute path.

    The path is resolved (symlinks collapsed, made absolute) so that aliases of
    the same directory hash identically. The result is pure lowercase hex, making
    it inherently filesystem-safe (no ``/``, ``..``, or leading slash).
    """
    resolved = str(Path(path).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
