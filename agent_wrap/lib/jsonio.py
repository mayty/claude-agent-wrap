# This file has been created with the assistance of an AI tool.
"""Reading JSON that somebody else owns, where a bad document is not an error."""

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def read_json_object(path: Path) -> dict[str, Any] | None:
    """
    Read a JSON object from *path*: ``{}`` when blank, ``None`` when unreadable or not
    an object.

    ``None`` is the "leave it alone" answer. Every caller here is editing a file a user
    or another tool wrote, so a document this process cannot make sense of is one it must
    not overwrite. A blank file is different -- it carries no content to lose.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    if not text.strip():
        return {}
    try:
        parsed: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def json_object(raw: str) -> dict[str, object]:
    """Parse *raw* as a JSON object; ``{}`` on anything else."""
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): value for key, value in parsed.items()}


def json_array(raw: str) -> list[object]:
    """Parse *raw* as a JSON array; ``[]`` on anything else."""
    try:
        parsed: object = json.loads(raw)
    except json.JSONDecodeError, ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    return list(parsed)
