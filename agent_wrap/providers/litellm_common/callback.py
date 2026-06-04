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

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# The host log directory is bind-mounted here by the provider lifecycle
# (see litellm_common/provider.py::_start). The provider-specific subdirectory
# is mounted directly to /var/log/agent-wrap, so we only need to append the session_id.


def _get_session_id(kwargs: dict[str, Any]) -> str:
    """Extract the Claude Code session ID from the proxy server request headers."""
    litellm_params = kwargs.get("litellm_params") or {}
    proxy_request = litellm_params.get("proxy_server_request") or {}
    headers = proxy_request.get("headers") or {}
    return headers.get("x-claude-code-session-id", "unknown-session")


def _json_safe(obj: Any, _seen: frozenset[int] = frozenset()) -> Any:
    """
    Recursively coerce ``obj`` into JSON-serializable primitives.

    LiteLLM's request/response objects contain cycles (e.g. an object that
    references the logging object that references it), which makes a plain
    ``json.dumps(..., default=str)`` raise "Circular reference detected". This
    walks the structure, tracking container ids on the current path, and
    replaces any back-reference with ``"<circular>"``. Unknown leaf types fall
    back to ``str()``.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, (dict, list, tuple, set)):
        if id(obj) in _seen:
            return "<circular>"
        seen = _seen | {id(obj)}
        if isinstance(obj, dict):
            return {str(k): _json_safe(v, seen) for k, v in obj.items()}
        return [_json_safe(v, seen) for v in obj]
    # Pydantic models and other objects: prefer a dict view if available.
    for attr in ("model_dump", "dict"):
        method = getattr(obj, attr, None)
        if callable(method):
            try:
                return _json_safe(method(), _seen)
            except Exception:  # noqa: BLE001 - best-effort, fall through to str()
                break
    return str(obj)


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
    leaves.
    """
    litellm_params = kwargs.get("litellm_params") or {}
    record: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "model": _json_safe(kwargs.get("model")),
        "request": {
            "messages": _json_safe(kwargs.get("messages")),
            "proxy_server_request": _json_safe(litellm_params.get("proxy_server_request")),
        },
        "response": _json_safe(response_obj),
    }
    if exc is not None:
        record["error"] = str(exc)
    return record


def _write_record(record: dict[str, Any], kwargs: dict[str, Any]) -> None:
    """Append one record as a JSON line to the session-specific log file. Never raises (logging must not break the proxy)."""
    try:
        session_id = _get_session_id(kwargs)
        log_dir = Path(f"/var/log/agent-wrap/{session_id}")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "messages.jsonl"

        line = json.dumps(record, default=str)
        with log_file.open("a") as f:
            f.write(line + "\n")
    except Exception as e:  # noqa: BLE001 - logging is best-effort
        print(f"agent-wrap callback: failed to write log record: {e}", file=sys.stderr)


try:
    # litellm is only installed inside the sidecar container, not the dev env.
    from litellm.integrations.custom_logger import CustomLogger  # pyrefly: ignore[missing-import]

    class FileLogger(CustomLogger):
        """LiteLLM CustomLogger that appends each call to the JSONL log file."""

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:  # noqa: ARG002
            _write_record(build_record(kwargs, response_obj, status="success"), kwargs)

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:  # noqa: ARG002
            _write_record(
                build_record(
                    kwargs,
                    response_obj,
                    status="failure",
                    exc=kwargs.get("exception"),
                ),
                kwargs,
            )

    file_logger_instance = FileLogger()
except ImportError:
    # litellm isn't installed in this interpreter (e.g. running the repo's unit
    # tests). build_record stays importable; the callback instance is absent.
    file_logger_instance = None
