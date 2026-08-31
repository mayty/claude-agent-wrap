# This file has been created with the assistance of an AI tool.
"""Hash-pointer resolution for log records."""

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def load_strings(session_dir: Path) -> dict[str, str]:
    """Load a session's ``strings.jsonl`` into a ``{hash: original}`` map."""
    strings: dict[str, str] = {}
    strings_file = session_dir / "strings.jsonl"
    if not strings_file.is_file():
        return strings
    try:
        with strings_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                h = entry.get("hash")
                if isinstance(h, str) and "original" in entry:
                    strings[h] = entry["original"]
    except OSError:
        pass
    return strings


def resolve_hashes(obj: Any, strings: dict[str, str]) -> Any:
    """
    Replace ``hash:<sha256>`` strings with their originals.

    A lightweight tree walk. Unknown hashes are left intact so a missing
    ``strings.jsonl`` entry is visible rather than silently blanked.
    """
    if isinstance(obj, str):
        return strings.get(obj, obj)
    if isinstance(obj, dict):
        return {k: resolve_hashes(v, strings) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_hashes(v, strings) for v in obj]
    return obj
