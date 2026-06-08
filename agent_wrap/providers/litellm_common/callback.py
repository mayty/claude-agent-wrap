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
import re
import sys
from pathlib import Path
from typing import Any, TypedDict

try:
    from .helpers import get_response_content_str, get_session_hasher, json_safe
except ImportError:
    # Fallback for sidecar container execution where callback.py is mounted
    # as a top-level module in /etc/litellm/ alongside helpers.py
    _current_dir = str(Path(__file__).parent.resolve())
    if _current_dir not in sys.path:
        sys.path.insert(0, _current_dir)
    from helpers import (  # type: ignore[no-redef]
        get_response_content_str,
        get_session_hasher,
        json_safe,
    )


class MetaData(TypedDict):
    count: int
    last_ts: str
    models: list[str]
    alias: str | None
    title: str | None


class RequestLog(TypedDict):
    messages: list[dict[str, Any]]
    proxy_server_request: dict[str, Any]


class LogRecord(TypedDict):
    ts: str
    end_ts: str
    status: str
    model: str
    request: RequestLog
    response: dict[str, Any]
    error: str | None


# The host log directory is bind-mounted here by the provider lifecycle
# (see litellm_common/provider.py::_start). The provider-specific subdirectory
# is mounted directly to /var/log/agent-wrap, so we only need to append the session_id.


_ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')


def _get_session_id(kwargs: dict[str, Any]) -> str:
    """Extract the Claude Code session ID from the proxy server request headers."""
    litellm_params = kwargs.get("litellm_params") or {}
    proxy_request = litellm_params.get("proxy_server_request") or {}
    headers = proxy_request.get("headers") or {}
    return headers.get("x-claude-code-session-id", "unknown-session")


def build_record(  # noqa: PLR0913
    kwargs: dict[str, Any],
    response_obj: Any,
    status: str,
    exc: Any = None,
    *,
    start_ts: Any = None,
    end_ts: Any = None,
) -> LogRecord:
    """
    Build a JSON-serializable log record from a LiteLLM callback's arguments.

    Pure function (no I/O) so it can be unit-tested directly. All values are run
    through ``json_safe`` so the result has no cycles and no non-serializable
    leaves. String values meeting the length threshold are replaced with
    "hash:<sha256_hex>" format to reduce space bloat. The self-referencing
    ``body.proxy_server_request`` key is deleted before serialization to
    break the only cycle in LiteLLM's data structure.
    """
    session_id = _get_session_id(kwargs)
    hasher = get_session_hasher(session_id)

    litellm_params = kwargs.get("litellm_params") or {}
    psr = litellm_params.get("proxy_server_request")

    # Break the self-cycle: LiteLLM's proxy_server_request.body contains a key
    # ("proxy_server_request") that points back to the parent dict.  Deleting it
    # makes the structure a DAG, which json.dumps handles without issue.
    if isinstance(psr, dict) and isinstance(psr.get("body"), dict):
        psr["body"].pop("proxy_server_request", None)

    model = kwargs.get("model")
    record: LogRecord = {
        "ts": start_ts.isoformat(),
        "end_ts": end_ts.isoformat(),
        "status": status,
        "model": model or "undefined",
        "request": {
            "messages": json_safe(kwargs.get("messages"), hasher),
            "proxy_server_request": json_safe(psr, hasher),
        },
        "response": json_safe(response_obj, hasher),
        "error": None,
    }
    if exc is not None:
        record["error"] = hasher.hash_string(str(exc))

    # Flush the hasher to persist string mappings after building the record
    hasher.flush(session_id)

    return record


def extract_session_alias(response: Any) -> str | None:
    """
    Return Claude Code's kebab-case session name if this is its naming call.

    Claude Code's session-naming request flows through the proxy like any other
    call; its response content is a JSON object ``{"name": "<kebab-slug>"}``.
    The sibling title-generation call returns ``{"title": ...}`` and is ignored.
    The slug is short, so it is never hashed — this operates on the JSON-safe
    response dict directly. Returns None for anything that isn't a name payload.
    """
    content = get_response_content_str(response)
    if not content:
        return None
    stripped = content.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        # Tolerate JSON-ish-but-not-strict content (e.g. trailing prose).
        match = _ALIAS_NAME_RE.search(stripped)
        return match.group(1).strip() or None if match else None
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def extract_session_title(response: Any) -> str | None:
    """
    Return Claude Code's sentence-case session title if this is its title call.

    Claude Code generates a session title via a small model call whose response
    content is ``{"title": "…"}``.  This mirrors :func:`extract_session_alias`
    but for the sibling title payload.  Returns None for anything else.
    """
    content = get_response_content_str(response)
    if not content:
        return None
    stripped = content.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        match = _TITLE_RE.search(stripped)
        return match.group(1).strip() or None if match else None
    if isinstance(obj, dict):
        title = obj.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def _get_empty_meta() -> MetaData:
    return {
        "count": 0,
        "last_ts": "0000-00-00T00:00:00+00:00",
        "models": [],
        "alias": None,
        "title": None,
    }


def _read_meta(log_dir: Path) -> MetaData:
    """Read existing ``meta.json``, returning ``{}`` if missing or corrupt."""
    meta_file = log_dir / "meta.json"
    if not meta_file.is_file():
        return _get_empty_meta()
    try:
        return json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _get_empty_meta()


def _write_meta(log_dir: Path, meta: MetaData) -> None:
    """Write ``meta.json`` atomically.  Best-effort; never raises."""
    meta_file = log_dir / "meta.json"
    tmp_file = log_dir / "meta.json.tmp"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        tmp_file.write_text(json.dumps(meta), encoding="utf-8")
        tmp_file.replace(meta_file)
    except OSError:
        pass


def _get_log_dir(kwargs: dict[str, Any]) -> Path:
    """Return the per-session log directory for the session in *kwargs*."""
    return Path(f"/var/log/agent-wrap/{_get_session_id(kwargs)}")


async def _write_record_async(record: LogRecord, kwargs: dict[str, Any]) -> None:
    """Append *record* as a JSON line to ``messages.jsonl``.  Never raises."""
    session_id = _get_session_id(kwargs)

    # Flush string mappings before appending the record.
    hasher = get_session_hasher(session_id)
    await asyncio.to_thread(hasher.flush, session_id)

    log_dir = _get_log_dir(kwargs)
    log_file = log_dir / "messages.jsonl"
    line = json.dumps(record, default=str)

    def _append() -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_append)


def _write_metadata(record: LogRecord, kwargs: dict[str, Any]) -> None:
    """Update ``meta.json`` from *record*.  Never raises."""
    log_dir = _get_log_dir(kwargs)

    log_dir.mkdir(parents=True, exist_ok=True)

    meta = _read_meta(log_dir)
    meta["count"] += 1
    if record.get("ts"):
        meta["last_ts"] = record["end_ts"]
    model = record.get("model")
    if model:
        short = model.rsplit("/", 1)[-1]
        meta["models"] = sorted(set(meta["models"]) | {short})
    alias = extract_session_alias(record.get("response"))
    if alias:
        meta["alias"] = alias
    title = extract_session_title(record.get("response"))
    if title:
        meta["title"] = title
    _write_meta(log_dir, meta)


def _resolve_thinking_reasoning_conflict(data: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve API conflict between ``thinking`` and ``reasoning_effort``.

    Some providers (DeepSeek's Anthropic-compatible API) rejects requests where
    ``thinking.type`` is ``"disabled"`` but reasoning_effort is also set.
    Claude Code sends this combination for lightweight calls (title generation,
    session naming) when ``CLAUDE_CODE_EFFORT_LEVEL`` is configured.

    This hook strips ``reasoning_effort`` when thinking is explicitly disabled,
    preserving Claude Code's intent that the call should proceed without
    extended thinking.
    """
    if not isinstance(data.get("thinking"), dict) or data["thinking"].get("type") != "disabled":
        return data

    if not isinstance(data.get("output_config"), dict):
        return data

    data["output_config"].pop("effort", None)

    return data


try:
    # litellm is only installed inside the sidecar container, not the dev env.
    from litellm.integrations.custom_logger import CustomLogger  # pyrefly: ignore[missing-import]

    class FileLogger(CustomLogger):
        """LiteLLM CustomLogger that appends each call to the JSONL log file asynchronously."""

        async def async_pre_call_hook(
            self,
            user_api_key_dict,  # noqa: ARG002
            cache,  # noqa: ARG002
            data: dict,
            call_type: str,  # noqa: ARG002
        ) -> dict:
            """Resolve provider-specific parameter conflicts before the upstream call."""
            return _resolve_thinking_reasoning_conflict(data)

        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time) -> None:
            record = build_record(
                kwargs, response_obj, status="success", start_ts=start_time, end_ts=end_time
            )
            await _write_record_async(record, kwargs)
            _write_metadata(record, kwargs)

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time) -> None:
            record = build_record(
                kwargs,
                response_obj,
                status="failure",
                exc=kwargs.get("exception"),
                start_ts=start_time,
                end_ts=end_time,
            )
            try:
                await _write_record_async(record, kwargs)
            except Exception as e:  # noqa: BLE001 - logging is best-effort
                print(f"agent-wrap callback: failed to write log record: {e}", file=sys.stderr)
            try:
                _write_metadata(record, kwargs)

            except Exception as e:  # noqa: BLE001 - logging is best-effort
                print(f"agent-wrap callback: failed to write metadata: {e}", file=sys.stderr)

    file_logger_instance = FileLogger()
except ImportError:
    # litellm isn't installed in this interpreter (e.g. running the repo's unit
    # tests). build_record stays importable; the callback instance is absent.
    file_logger_instance = None
