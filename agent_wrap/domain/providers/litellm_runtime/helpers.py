from __future__ import annotations

import sys
from collections.abc import Mapping
from copy import copy
from pathlib import Path
from typing import Any

# When mounted into the sidecar container, helpers.py sits at /etc/litellm/
# alongside string_hasher.py — not inside a Python package. Add the current
# directory to sys.path so the import resolves.
_current_dir = str(Path(__file__).parent.resolve())
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
from string_hasher import StringHasher  # noqa: E402  # pyrefly: ignore [missing-import]

# Global cache of hashers per session to enable cross-request deduplication
# and prevent concurrent flushes from writing duplicate mappings.
_SESSION_HASHERS: dict[str, StringHasher] = {}

#: Mapping keys whose value is a live credential and must never reach the log.
#: LiteLLM hands the callback the client's headers in more than one place — the
#: top-level ``proxy_server_request.headers``, and (on the router routes) again
#: inside ``body.secret_fields.raw_headers`` — so redaction happens here, at the
#: single serialization boundary every record passes through, rather than at each
#: call site. Compared lowercased.
REDACTED_HEADERS = frozenset(
    {
        "authorization",
        "x-litellm-api-key",
        "x-api-key",
        "api-key",
    }
)
REDACTED_VALUE = "<redacted>"


def get_session_hasher(session_id: str, log_dir: Path) -> StringHasher:
    """
    Get or create a StringHasher for a session, loading existing state.

    The cache is keyed by ``session_id`` (Claude Code's globally-unique session
    UUID) for cross-request deduplication, but I/O uses ``log_dir`` — the
    resolved per-project/provider/session directory — so ``strings.jsonl`` lands
    next to ``messages.jsonl`` under the shared sidecar's mount.
    """
    if session_id not in _SESSION_HASHERS:
        hasher = StringHasher()
        hasher.load_seen_hashes(log_dir)
        _SESSION_HASHERS[session_id] = hasher
    return _SESSION_HASHERS[session_id]


def json_safe(  # noqa: PLR0911
    obj: Any,
    _hasher: StringHasher | None = None,
    visited: set[int] | None = None,
) -> Any:
    """
    Recursively coerce ``obj`` into JSON-serializable primitives.

    Callers must ensure the object graph has no cycles (e.g. by deleting
    self-referencing keys like ``proxy_server_request.body.proxy_server_request``
    before calling). Shared references are serialized normally — duplicated in
    the output, which ``json.dumps`` handles without issue.

    Unknown leaf types fall back to ``str()``. If ``_hasher`` is provided,
    string values meeting the length threshold are replaced with
    ``"hash:<sha256_hex>"``.

    Every mapping is serialized as an object, not just ``dict``: LiteLLM's
    ``/anthropic/*`` passthrough route puts a Starlette ``Headers`` mapping in
    ``proxy_server_request["headers"]``, which used to hit the ``str()`` fallback
    and collapse the whole header set into one opaque ``"Headers({...})"`` blob —
    losing the ``x-claude-code-agent-id`` the logs viewer needs to separate
    subagent threads. Values under a :data:`REDACTED_HEADERS` key are replaced
    with :data:`REDACTED_VALUE`.
    """
    if visited is None:
        visited = set()
    elif id(obj) in visited:
        return "<recursive_record>"

    visited.add(id(obj))

    # Handle primitive types
    if obj is None or isinstance(obj, (int, float, bool)):
        return obj

    # Handle strings (with optional hashing)
    if isinstance(obj, str):
        return _hasher.hash_string(obj) if _hasher else obj

    # Handle containers
    if isinstance(obj, Mapping):
        return {
            str(k): (
                REDACTED_VALUE
                if str(k).lower() in REDACTED_HEADERS
                else json_safe(v, _hasher, copy(visited))
            )
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple, set)):
        return [json_safe(v, _hasher, copy(visited)) for v in obj]

    # Handle Pydantic models and other objects with model_dump/dict methods
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return json_safe(method(), _hasher, copy(visited))
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
