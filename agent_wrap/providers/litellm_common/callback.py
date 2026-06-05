# This file has been created with the assistance of an AI tool.
"""
LiteLLM custom callback that logs every LLM call to a JSONL file.

Mounted into the shared sidecar next to the config (``/etc/litellm/callback.py``)
and referenced from each provider's ``config.yaml`` as
``callback.file_logger_instance``. LiteLLM resolves the callback module relative
to the config file's directory, so the file must sit beside ``config.yaml``.

The callback runs in-process inside the sidecar and appends one JSON object per
call (request + response) to ``LOG_FILE``. There is no separate backend, HTTP
hop, or database — this is a minimal "see what the agent sent upstream" log for
proof-of-concept use. Logging failures are swallowed so they can never break the
proxy.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .string_hasher import StringHasher, get_session_hasher
except ImportError:
    # Fallback for sidecar container execution where callback.py is mounted
    # as a top-level module in /etc/litellm/ alongside string_hasher.py
    # LiteLLM loads this module via importlib.util.spec_from_file_location,
    # which does not automatically add the file's directory to sys.path.
    # We must add it explicitly so that `string_hasher` can be imported
    # when running inside the sidecar container.
    _current_dir = str(Path(__file__).parent.resolve())
    if _current_dir not in sys.path:
        sys.path.insert(0, _current_dir)
    from string_hasher import StringHasher, get_session_hasher  # type: ignore[no-redef]

# The host log directory is bind-mounted here by the provider lifecycle
# (see litellm_common/provider.py::_start). The provider-specific subdirectory
# is mounted directly to /var/log/agent-wrap, so we only need to append the session_id.


def _get_session_id(kwargs: dict[str, Any]) -> str:
    """Extract the Claude Code session ID from the proxy server request headers."""
    litellm_params = kwargs.get("litellm_params") or {}
    proxy_request = litellm_params.get("proxy_server_request") or {}
    headers = proxy_request.get("headers") or {}
    return headers.get("x-claude-code-session-id", "unknown-session")


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
            result = {str(k): _json_safe(v, tracker, _hasher) for k, v in obj.items()}
            result["wrap-ref-id"] = ref_id
            return result

        # For list/tuple/set, convert to list first
        result = [_json_safe(v, tracker, _hasher) for v in obj]
        result.insert(0, f"wrap-ref-id:{ref_id}")
        return result

    # Not referenced multiple times, serialize normally without ref_id
    if isinstance(obj, dict):
        return {str(k): _json_safe(v, tracker, _hasher) for k, v in obj.items()}
    return [_json_safe(v, tracker, _hasher) for v in obj]


def _json_safe(
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
                return _json_safe(method(), tracker, _hasher)
            except Exception:  # noqa: BLE001 - best-effort, fall through to str()
                break

    # Fallback for unknown types: convert to string and optionally hash
    str_val = str(obj)
    return _hasher.hash_string(str_val) if _hasher else str_val


def build_record(
    kwargs: dict[str, Any],
    response_obj: Any,
    status: str,
    exc: Any = None,
) -> dict[str, Any]:
    """
    Build a JSON-serializable log record from a LiteLLM callback's arguments.

    Pure function (no I/O) so it can be unit-tested directly. All values are run
    through ``_json_safe`` so the result has no cycles and no non-serializable
    leaves. String values meeting the length threshold are replaced with
    "hash:<sha256_hex>" format to reduce space bloat. Circular references are
    replaced with "wrap-ref:<id>" and the canonical object is annotated with its
    reference ID.
    """
    session_id = _get_session_id(kwargs)
    hasher = get_session_hasher(session_id)

    # Pass a dict containing all top-level objects to the tracker so it can
    # find references across any of them.
    tracker = RefTracker(
        {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages"),
            "proxy_server_request": kwargs.get("litellm_params", {}).get("proxy_server_request"),
            "response": response_obj,
        }
    )

    litellm_params = kwargs.get("litellm_params") or {}
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "model": _json_safe(kwargs.get("model"), tracker, hasher),
        "request": {
            "messages": _json_safe(kwargs.get("messages"), tracker, hasher),
            "proxy_server_request": _json_safe(
                litellm_params.get("proxy_server_request"), tracker, hasher
            ),
        },
        "response": _json_safe(response_obj, tracker, hasher),
    }
    if exc is not None:
        record["error"] = hasher.hash_string(str(exc))

    # Flush the hasher to persist string mappings after building the record
    hasher.flush(session_id)

    return record


async def _write_record_async(record: dict[str, Any], kwargs: dict[str, Any]) -> None:
    """Append one record as a JSON line to the session-specific log file asynchronously. Never raises."""
    try:
        session_id = _get_session_id(kwargs)

        # Get the hasher (this triggers _load_seen_hashes if it's the first time)
        hasher = get_session_hasher(session_id)

        # Flush string mappings in a background thread
        await asyncio.to_thread(hasher.flush, session_id)

        # Append the log record in a background thread
        log_dir = Path(f"/var/log/agent-wrap/{session_id}")
        log_file = log_dir / "messages.jsonl"
        line = json.dumps(record, default=str)

        def _append_to_file() -> None:
            log_dir.mkdir(parents=True, exist_ok=True)
            with log_file.open("a", encoding="utf-8") as f:
                f.write(line + "\n")

        await asyncio.to_thread(_append_to_file)

    except Exception as e:  # noqa: BLE001 - logging is best-effort
        print(f"agent-wrap callback: failed to write log record: {e}", file=sys.stderr)


try:
    # litellm is only installed inside the sidecar container, not the dev env.
    from litellm.integrations.custom_logger import CustomLogger  # pyrefly: ignore[missing-import]

    class FileLogger(CustomLogger):
        """LiteLLM CustomLogger that appends each call to the JSONL log file asynchronously."""

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:  # noqa: ARG002
            record = build_record(kwargs, response_obj, status="success")
            await _write_record_async(record, kwargs)

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:  # noqa: ARG002
            record = build_record(
                kwargs,
                response_obj,
                status="failure",
                exc=kwargs.get("exception"),
            )
            await _write_record_async(record, kwargs)

    file_logger_instance = FileLogger()
except ImportError:
    # litellm isn't installed in this interpreter (e.g. running the repo's unit
    # tests). build_record stays importable; the callback instance is absent.
    file_logger_instance = None
