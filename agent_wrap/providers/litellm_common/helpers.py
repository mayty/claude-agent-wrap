from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    from .string_hasher import StringHasher
except ImportError:
    # Fallback for sidecar container execution where helpers.py is mounted
    # as a top-level module in /etc/litellm/ alongside string_hasher.py
    _current_dir = str(Path(__file__).parent.resolve())
    if _current_dir not in sys.path:
        sys.path.insert(0, _current_dir)
    from string_hasher import StringHasher  # type: ignore[no-redef]


# Global cache of hashers per session to enable cross-request deduplication
# and prevent concurrent flushes from writing duplicate mappings.
_SESSION_HASHERS: dict[str, StringHasher] = {}


def get_session_hasher(session_id: str) -> StringHasher:
    """Get or create a StringHasher for a specific session, loading existing state."""
    if session_id not in _SESSION_HASHERS:
        hasher = StringHasher()
        hasher.load_seen_hashes(session_id)
        _SESSION_HASHERS[session_id] = hasher
    return _SESSION_HASHERS[session_id]


class RefTracker:
    """Tracks object references to replace circular dependencies with reference IDs."""

    def __init__(self, root_obj: Any) -> None:
        self.assigned_ids: dict[int, str] = {}
        self.reference_counts: dict[int, int] = {}
        self.next_id: int = 0
        self._count_references(root_obj)

    def _count_references(self, obj: Any) -> None:
        stack: list[Any] = [obj]
        seen_counts: set[int] = set()

        while stack:
            current = stack.pop()
            if isinstance(current, (dict, list, tuple, set)):
                obj_id = id(current)
                self.reference_counts[obj_id] = self.reference_counts.get(obj_id, 0) + 1

                if obj_id in seen_counts:
                    continue
                seen_counts.add(obj_id)

                if isinstance(current, dict):
                    stack.extend(current.values())
                else:
                    stack.extend(current)

    def get_id(self, obj: Any) -> str | None:
        return self.assigned_ids.get(id(obj))

    def assign_id(self, obj: Any) -> str:
        ref_id = str(self.next_id)
        self.next_id += 1
        self.assigned_ids[id(obj)] = ref_id
        return ref_id

    def is_referenced(self, obj: Any) -> bool:
        return self.reference_counts.get(id(obj), 0) > 1


def _json_safe_container(
    obj: dict | list | tuple | set,
    tracker: RefTracker,
    _hasher: StringHasher | None = None,
) -> Any:
    """Handle JSON serialization for containers with reference tracking."""
    if tracker.is_referenced(obj):
        existing_ref = tracker.get_id(obj)
        if existing_ref is not None:
            return f"wrap-ref:{existing_ref}"

        # First time serializing this referenced container
        ref_id = tracker.assign_id(obj)

        if isinstance(obj, dict):
            result = {str(k): json_safe(v, tracker, _hasher) for k, v in obj.items()}
            result["wrap-ref-id"] = ref_id
            return result

        # For list/tuple/set, convert to list first
        result = [json_safe(v, tracker, _hasher) for v in obj]
        result.insert(0, f"wrap-ref-id:{ref_id}")
        return result

    # Not referenced multiple times, serialize normally without ref_id
    if isinstance(obj, dict):
        return {str(k): json_safe(v, tracker, _hasher) for k, v in obj.items()}
    return [json_safe(v, tracker, _hasher) for v in obj]


def json_safe(
    obj: Any,
    tracker: RefTracker,
    _hasher: StringHasher | None = None,
) -> Any:
    """
    Recursively coerce ``obj`` into JSON-serializable primitives.

    LiteLLM's request/response objects contain cycles (e.g. an object that
    references the logging object that references it), which makes a plain
    ``json.dumps(..., default=str)`` raise "Circular reference detected". This
    walks the structure, assigning a unique reference ID to each container on
    first encounter *only if the container is referenced multiple times*.
    Subsequent encounters of the same container are replaced with ``"wrap-ref:<id>"``.
    The reference ID is also injected into the serialized container (as a
    ``wrap-ref-id`` key for dicts, or at index 0 for lists). Unknown leaf types fall
    back to ``str()``. If ``_hasher`` is provided, string values meeting the
    length threshold are replaced with ``"hash:<sha256_hex>"``.
    """
    # Handle primitive types
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj

    # Handle strings (with optional hashing)
    if isinstance(obj, str):
        return _hasher.hash_string(obj) if _hasher else obj

    # Handle containers
    if isinstance(obj, (dict, list, tuple, set)):
        return _json_safe_container(obj, tracker, _hasher)

    # Handle Pydantic models and other objects with model_dump/dict methods
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return json_safe(method(), tracker, _hasher)
            except Exception:  # noqa: BLE001 - best-effort, fall through to str()
                break

    # Fallback for unknown types: convert to string and optionally hash
    str_val = str(obj)
    return _hasher.hash_string(str_val) if _hasher else str_val


def get_response_content_str(response: Any) -> str | None:
    """
    Pull the assistant's text content out of a JSON-safe response dict.

    Handles the OpenAI-shaped ``choices[0].message.content`` and the older
    ``choices[0].text`` variant. Returns None when no string content is found.
    """
    if not isinstance(response, dict):
        return None
    choices = response.get("choices")
    if not (isinstance(choices, list) and choices):
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(first.get("text"), str):
        return first["text"]
    return None
