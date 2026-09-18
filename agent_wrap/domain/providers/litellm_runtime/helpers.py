# This file has been edited with the assistance of an AI tool.
import sys
from collections.abc import Mapping
from copy import copy
from pathlib import Path
from typing import Any

# Mounted into the sidecar at /etc/litellm/ alongside string_hasher.py, not as a
# package, so the plain import below needs this directory on sys.path.
_current_dir = str(Path(__file__).parent.resolve())
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
from string_hasher import StringHasher  # noqa: E402  # pyrefly: ignore [missing-import]

# Keyed by session so hashing dedupes across requests, and so concurrent flushes
# of one session go through a single hasher.
_SESSION_HASHERS: dict[str, StringHasher] = {}

#: Mapping keys whose value is a live credential and must never reach the log.
#: LiteLLM hands the callback the client's headers in more than one place — the
#: top-level ``proxy_server_request.headers``, and (on the router routes) again
#: inside ``body.secret_fields.raw_headers`` — so redaction happens here, at the
#: single serialization boundary every record passes through. Compared lowercased.
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

    Keyed by *session_id* but doing I/O under *log_dir*: the two differ, and only
    *log_dir* puts ``strings.jsonl`` beside the ``messages.jsonl`` it describes.
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
    before calling).

    Every *Mapping* is serialized as an object, not just ``dict``: LiteLLM's
    ``/anthropic/*`` passthrough route puts a Starlette ``Headers`` mapping in
    ``proxy_server_request["headers"]``, and the ``str()`` fallback would collapse
    the whole header set into one opaque ``"Headers({...})"`` blob — losing the
    ``x-claude-code-agent-id`` the logs viewer needs to separate subagent threads.
    """
    if visited is None:
        visited = set()
    elif id(obj) in visited:
        return "<recursive_record>"

    visited.add(id(obj))

    if obj is None or isinstance(obj, (int, float, bool)):
        return obj

    if isinstance(obj, str):
        return _hasher.hash_string(obj) if _hasher else obj

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

    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return json_safe(method(), _hasher, copy(visited))
            except Exception:  # noqa: BLE001 - best-effort, fall through to str()
                break

    str_val = str(obj)
    return _hasher.hash_string(str_val) if _hasher else str_val
