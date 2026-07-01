# This file has been created with the assistance of an AI tool.
"""Stable path hashing for per-project log segregation."""

from __future__ import annotations

import hashlib
from pathlib import Path


def project_path_hash(path: Path) -> str:
    """
    Return a stable 16-hex-char SHA-256 of a project's fully-resolved path.

    Used to segregate per-project LiteLLM logs under the shared sidecar's single
    mounted directory. The path is resolved (symlinks collapsed, made absolute)
    so that aliases of the same directory hash identically; this must match how
    both the symlink target and the injected request header are derived. The
    result is pure lowercase hex, so it is inherently filesystem-safe (no '/',
    '..', or leading slash) and the user never sees it.
    """
    resolved = str(Path(path).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:16]
