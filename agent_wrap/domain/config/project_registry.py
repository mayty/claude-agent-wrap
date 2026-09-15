# This file has been created with the assistance of an AI tool.
"""
The pre-SQLite ``projects.txt`` encoding — kept for the one-time import.

The registry now lives in a database, one row per path. This module is what decodes the
compressed, grouped text format it used to live in (``/a/{x,y,z}`` sibling groups,
``{N}/rest`` prefix borrows), so ``ConfigService._import_legacy_registry`` can read an
existing file once and move it aside.

Used by ``ConfigService`` — not part of the public domain API.
"""

from pathlib import PurePosixPath

from agent_wrap.domain.config.constants import PREFIX_RE, SIBLING_RE


class ProjectRegistry:
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
    def _try_expand_siblings(path: str) -> list[str] | None:
        m = SIBLING_RE.search(path)
        if m and "," in m.group(1):
            parent = path[: m.start()]
            leaves = m.group(1).split(",")
            return [f"{parent}/{leaf}" for leaf in leaves]
        return None
