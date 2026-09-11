# This file has been created with the assistance of an AI tool.
"""
The pre-SQLite ``projects.txt`` encoding — kept for the one-time import.

The registry now lives in a database, one row per path. This module is what decodes the
compressed, grouped text format it used to live in (``/a/{x,y,z}`` sibling groups,
``{N}/rest`` prefix borrows), so ``ConfigService._import_legacy_registry`` can read an
existing file once and move it aside. ``compress`` has no remaining caller and is kept
only as the round-trip partner the tests check ``decompress`` against.

Used by ``ConfigService`` — not part of the public domain API.
"""

from pathlib import PurePosixPath

from agent_wrap.domain.config.constants import MIN_SIBLING_RUN, PREFIX_RE, SIBLING_RE
from agent_wrap.domain.config.models import Entry


class ProjectRegistry:
    @staticmethod
    def compress(paths: list[str]) -> list[str]:
        if not paths:
            return []

        paths = sorted(set(paths))

        entries: list[Entry] = []
        i = 0
        while i < len(paths):
            parent = PurePosixPath(paths[i]).parent
            j = i + 1
            while j < len(paths) and PurePosixPath(paths[j]).parent == parent:
                j += 1
            run = paths[i:j]
            if len(run) >= MIN_SIBLING_RUN:
                leaves = [PurePosixPath(p).name for p in run]
                sep = "" if str(parent) == "/" else "/"
                compressed = f"{parent}{sep}{{{','.join(leaves)}}}"
            else:
                compressed = run[0]
            entries.append(
                Entry(
                    compressed=compressed,
                    first_original=run[0],
                    last_original=run[-1],
                )
            )
            i = j

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

    @staticmethod
    def decompress(lines: list[str]) -> list[str]:
        result: list[str] = []
        last_path: str | None = None

        for raw in lines:
            line = raw.strip()
            if not line:
                continue

            # Resolve {N}/ prefix (Rule 1).
            m = PREFIX_RE.match(line)
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

    @staticmethod
    def _shared_segments(path_a: str, path_b: str) -> int:
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
        m = SIBLING_RE.search(path)
        if m and "," in m.group(1):
            parent = path[: m.start()]
            leaves = m.group(1).split(",")
            return [f"{parent}/{leaf}" for leaf in leaves]
        return None
