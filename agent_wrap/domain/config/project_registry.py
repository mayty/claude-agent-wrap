# This file has been created with the assistance of an AI tool.
"""
Internal path compression helpers for the project registry.

Used by ``ConfigService`` — not part of the public domain API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

# Matches a {N}/ prefix at the start of a line (N = non-zero digits).
_PREFIX_RE = re.compile(r"^\{(\d+)\}/(.*)$")

# Matches a terminal /{...} sibling group containing at least one comma.
_SIBLING_RE = re.compile(r"/\{([^}]+)\}$")

# Minimum run length to trigger sibling grouping.
_MIN_SIBLING_RUN = 2


@dataclass
class _Entry:
    """Intermediate representation used during compression."""

    compressed: str
    first_original: str
    last_original: str


class ProjectRegistry:
    """Internal helpers — called by ``ConfigService``."""

    # ------------------------------------------------------------------
    # compression
    # ------------------------------------------------------------------

    @staticmethod
    def compress(paths: list[str]) -> list[str]:
        """Compress a sorted, deduplicated list of absolute paths."""
        if not paths:
            return []

        paths = sorted(set(paths))

        # -- Pass 1: sibling grouping ---------------------------------
        entries: list[_Entry] = []
        i = 0
        while i < len(paths):
            parent = PurePosixPath(paths[i]).parent
            j = i + 1
            while j < len(paths) and PurePosixPath(paths[j]).parent == parent:
                j += 1
            run = paths[i:j]
            if len(run) >= _MIN_SIBLING_RUN:
                leaves = [PurePosixPath(p).name for p in run]
                sep = "" if str(parent) == "/" else "/"
                compressed = f"{parent}{sep}{{{','.join(leaves)}}}"
            else:
                compressed = run[0]
            entries.append(
                _Entry(
                    compressed=compressed,
                    first_original=run[0],
                    last_original=run[-1],
                )
            )
            i = j

        # -- Pass 2: prefix sharing -----------------------------------
        result: list[str] = [entries[0].compressed]
        for k in range(1, len(entries)):
            prev = entries[k - 1]
            cur = entries[k]
            shared = ProjectRegistry._shared_segments(prev.last_original, cur.first_original)
            if shared >= 1:
                parts = PurePosixPath(cur.compressed).parts
                remaining = "/".join(parts[1 + shared :])
                if remaining:
                    result.append(f"{{{shared}}}/{remaining}")
                    continue
            result.append(cur.compressed)

        return result

    # ------------------------------------------------------------------
    # decompression
    # ------------------------------------------------------------------

    @staticmethod
    def decompress(lines: list[str]) -> list[str]:
        """Expand compressed *lines* back to absolute paths."""
        result: list[str] = []
        last_path: str | None = None

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            # Resolve {N}/ prefix (Rule 1).
            m = _PREFIX_RE.match(line)
            if m:
                if last_path is None:
                    continue  # malformed — no previous path to borrow
                n = int(m.group(1))
                suffix = m.group(2)
                prefix_segments = PurePosixPath(last_path).parts[1 : 1 + n]
                path = "/" + "/".join(prefix_segments) + "/" + suffix
            else:
                path = line

            # Expand sibling group (Rule 2).
            expanded = ProjectRegistry._try_expand_siblings(path)
            if expanded is not None:
                result.extend(expanded)
                last_path = expanded[-1]
            else:
                result.append(path)
                last_path = path

        return result

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _shared_segments(path_a: str, path_b: str) -> int:
        """Return the number of non-root path components shared by two absolute paths."""
        parts_a = PurePosixPath(path_a).parts
        parts_b = PurePosixPath(path_b).parts
        n = 0
        for pa, pb in zip(parts_a[1:], parts_b[1:], strict=False):
            if pa == pb:
                n += 1
            else:
                break
        return n

    @staticmethod
    def _try_expand_siblings(path: str) -> list[str] | None:
        """Expand a terminal ``/{leaf,...}`` group, or return *None*."""
        m = _SIBLING_RE.search(path)
        if m and "," in m.group(1):
            parent = path[: m.start()]
            leaves = m.group(1).split(",")
            return [f"{parent}/{leaf}" for leaf in leaves]
        return None
