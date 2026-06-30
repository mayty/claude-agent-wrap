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
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

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


if TYPE_CHECKING:
    from .string_hasher import StringHasher

    class MetaData(TypedDict):
        count: int
        last_ts: float | None
        models: list[str]
        alias: str | None
        title: str | None

    class RequestTiming(TypedDict):
        start: float | None
        completionStart: float | None
        end: float | None

    class LogRecord(TypedDict):
        timing: RequestTiming
        status: str
        model: str
        request: dict[str, Any]
        response: dict[str, Any]
        error: str | None


# A single shared sidecar (first-launch-wins) serves every project on the host,
# so its log directory (bind-mounted to /var/log/agent-wrap by
# litellm_common/provider.py::_start) is project-independent. The callback routes
# each record to /var/log/agent-wrap/<project_hash>/<provider>/<session_id>/ where:
#   - <project_hash> varies per request and arrives in the x-agent-wrap-log-prefix
#     header (injected by the wrapper via Claude Code's ANTHROPIC_CUSTOM_HEADERS);
#   - <provider> is fixed per sidecar and arrives in the AGENT_WRAP_PROVIDER env var;
#   - <session_id> is Claude Code's own x-claude-code-session-id header.
# A per-project symlink (cwd/.claude/litellm-logs -> the <project_hash> subtree)
# lets the viewer read this layout unchanged.


_ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')

# project_path_hash output is lowercase hex; validating against that alphabet
# inherently rejects '/', '.', and '..', so no separate traversal check is needed.
_HASH_RE = re.compile(r"^[0-9a-f]+$")
# Provider names are lowercase Docker-style identifiers (e.g. "litellm-bedrock").
_PROVIDER_RE = re.compile(r"^[a-z0-9-]+$")
_DEFAULT_PROJECT_HASH = "unknown-project"
_DEFAULT_PROVIDER = "unknown-provider"


def _get_session_id(kwargs: dict[str, Any]) -> str:
    """Extract the Claude Code session ID from the proxy server request headers."""
    litellm_params = kwargs.get("litellm_params") or {}
    proxy_request = litellm_params.get("proxy_server_request") or {}
    headers = proxy_request.get("headers") or {}
    return headers.get("x-claude-code-session-id", "unknown-session")


def _get_project_hash(kwargs: dict[str, Any]) -> str:
    """
    Extract the project hash from the x-agent-wrap-log-prefix request header.

    The wrapper injects this via ANTHROPIC_CUSTOM_HEADERS. Anything that isn't
    pure lowercase hex (missing header, '/'-bearing, absolute, traversal) falls
    back to a fixed default so a malformed value can never escape the mount.
    """
    litellm_params = kwargs.get("litellm_params") or {}
    proxy_request = litellm_params.get("proxy_server_request") or {}
    headers = proxy_request.get("headers") or {}
    raw = headers.get("x-agent-wrap-log-prefix", "")
    return raw if _HASH_RE.match(raw) else _DEFAULT_PROJECT_HASH


def _get_provider() -> str:
    """
    Return the provider name from the AGENT_WRAP_PROVIDER sidecar env var.

    Fixed for the shared sidecar's lifetime. Falls back to a default when unset
    or containing characters outside the Docker-style identifier alphabet.
    """
    name = os.environ.get("AGENT_WRAP_PROVIDER", "")
    return name if _PROVIDER_RE.match(name) else _DEFAULT_PROVIDER


def _usage_from_slo(logging_object: dict[str, Any]) -> dict[str, Any] | None:
    """
    Synthesize a usage dict from LiteLLM's standard_logging_object token fields.

    The SLO exposes flat ``prompt_tokens``/``completion_tokens`` (and may carry a
    cache split). Returns a ``{"usage": {...}}`` dict the cost path can read, or
    None when the SLO holds no usable token counts.
    """
    if not isinstance(logging_object, dict):
        return None
    in_tokens = logging_object.get("prompt_tokens")
    out_tokens = logging_object.get("completion_tokens")
    cw_tokens = logging_object.get("cache_creation_input_tokens")
    cr_tokens = logging_object.get("cache_read_input_tokens")
    if not any((in_tokens, out_tokens, cw_tokens, cr_tokens)):
        return None
    usage: dict[str, Any] = {}
    if in_tokens:
        usage["prompt_tokens"] = in_tokens
    if out_tokens:
        usage["completion_tokens"] = out_tokens
    if cw_tokens:
        usage["cache_creation_input_tokens"] = cw_tokens
    if cr_tokens:
        usage["cache_read_input_tokens"] = cr_tokens
    return {"usage": usage}


def _usable_response(
    response_obj: Any,
    logging_object: dict[str, Any],
    hasher: StringHasher,
) -> Any:
    """
    Return the response to log, recovering usage from the SLO when it was lost.

    LiteLLM usually hands the success hook a parsed response dict, which we keep
    verbatim. But it sometimes passes a raw httpx ``Response`` object instead;
    ``json_safe`` then stringifies it (e.g. ``"<Response [200 OK]>"``) and the
    usage is lost, which silently under-counts cost in ``agent stats``. Only in
    that non-dict case do we fall back to ``standard_logging_object`` and tag the
    source, so the three outcomes — native / recovered / unrecoverable — stay
    distinguishable and greppable in the logs.
    """
    serialized = json_safe(response_obj, hasher)
    if isinstance(serialized, dict):
        return serialized  # native: a parsed response dict, kept verbatim

    # The response didn't serialize to a dict (the raw-Response case): its usage is
    # gone. Recover from the SLO, preserving its response *content* and only filling
    # in a usage block when the SLO response lacks one. Replacing the whole dict
    # would drop choices/message content that alias/title extraction and the viewer
    # still read (see get_response_content_str / extract_session_alias).
    slo_response = logging_object.get("response")
    recovered = json_safe(slo_response, hasher) if isinstance(slo_response, dict) else None
    if not isinstance(recovered, dict):
        recovered = None
    if recovered is None or not isinstance(recovered.get("usage"), dict):
        synthesized = _usage_from_slo(logging_object)
        if synthesized is not None:
            # Merge usage onto the content dict rather than supplanting it.
            recovered = {**(recovered or {}), "usage": synthesized["usage"]}

    if recovered is not None and isinstance(recovered.get("usage"), dict):
        recovered["_usage_source"] = "standard_logging_object"
        return recovered

    # Neither the response nor the SLO had usable usage: this request's cost is
    # genuinely lost. Mark it loudly (rather than silently as a $0 record), keep the
    # original serialized response, and capture the SLO we failed to recover from so
    # the failure is debuggable (empty? missing token keys? unexpected shape?).
    return {
        "_usage_source": "unrecoverable",
        "_raw_response": serialized,
        "_standard_logging_object": json_safe(logging_object, hasher),
    }


def _epoch(primary: Any, fallback: Any) -> float | None:
    """
    Epoch-seconds from LiteLLM's value, else the callback's datetime, else None.

    LiteLLM's ``standard_logging_object`` timestamps are usually epoch-seconds, but
    can be absent; the callback's own ``start_time`` / ``end_time`` arguments are
    ``datetime`` objects that always cover the call, so they serve as the fallback.
    Guaranteeing a non-None ``start`` keeps the stats reader from minting the
    timestamp-less ``"?"`` day-key.
    """
    if isinstance(primary, (int, float)):
        return float(primary)
    if isinstance(fallback, datetime):
        return fallback.timestamp()
    return None


def build_record(  # noqa: PLR0913
    kwargs: dict[str, Any],
    response_obj: Any,
    status: str,
    exc: Any = None,
    start_time: Any = None,
    end_time: Any = None,
) -> LogRecord:
    """
    Build a JSON-serializable log record from a LiteLLM callback's arguments.

    Pure function (no I/O) so it can be unit-tested directly. All values are run
    through ``json_safe`` so the result has no cycles and no non-serializable
    leaves. String values meeting the length threshold are replaced with
    "hash:<sha256_hex>" format to reduce space bloat. The self-referencing
    ``body.proxy_server_request`` key is deleted before serialization to
    break the only cycle in LiteLLM's data structure.

    The ``request`` field is the proxy server request itself; the real Anthropic
    request lives at ``request.body.data``.

    ``start_time`` / ``end_time`` are the callback's own ``datetime`` bounds, used
    as a fallback when the standard_logging_object lacks the corresponding epoch
    timestamp (see :func:`_epoch`).
    """
    session_id = _get_session_id(kwargs)
    log_dir = _get_log_dir(kwargs)
    hasher = get_session_hasher(session_id, log_dir)

    litellm_params = kwargs.get("litellm_params") or {}
    psr = litellm_params.get("proxy_server_request")

    # Break the self-cycle: LiteLLM's proxy_server_request.body contains a key
    # ("proxy_server_request") that points back to the parent dict.  Deleting it
    # makes the structure a DAG, which json.dumps handles without issue.
    if isinstance(psr, dict) and isinstance(psr.get("body"), dict):
        psr["body"].pop("proxy_server_request", None)

    logging_object = kwargs.get("standard_logging_object", {})
    model = kwargs.get("model")
    # Successful calls must retain usage for cost accounting; recover it from the
    # standard_logging_object when the raw response_obj doesn't serialize to a
    # usable usage dict (see _usable_response). Failures carry no usage to lose.
    if status == "success":
        response = _usable_response(response_obj, logging_object, hasher)
    else:
        response = json_safe(response_obj, hasher)
    record: LogRecord = {
        "timing": {
            "start": _epoch(logging_object.get("startTime"), start_time),
            "completionStart": _epoch(logging_object.get("completionStartTime"), start_time),
            "end": _epoch(logging_object.get("endTime"), end_time),
        },
        "status": status,
        "model": model or "undefined",
        "request": json_safe(psr, hasher),
        "response": response,
        "error": None,
    }
    if exc is not None:
        record["error"] = hasher.hash_string(str(exc))

    # Flush the hasher to persist string mappings after building the record
    hasher.flush(log_dir)

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
        "last_ts": None,
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
    """Return the per-project/provider/session log directory for *kwargs*."""
    return (
        Path("/var/log/agent-wrap")
        / _get_project_hash(kwargs)
        / _get_provider()
        / _get_session_id(kwargs)
    )


async def _write_record_async(record: LogRecord, kwargs: dict[str, Any]) -> None:
    """Append *record* as a JSON line to ``messages.jsonl``.  Never raises."""
    session_id = _get_session_id(kwargs)
    log_dir = _get_log_dir(kwargs)

    # Flush string mappings before appending the record.
    hasher = get_session_hasher(session_id, log_dir)
    await asyncio.to_thread(hasher.flush, log_dir)

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
    end = (record.get("timing") or {}).get("end")
    if end is not None:
        meta["last_ts"] = end
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
            data: dict[str, Any],
            call_type: str,  # noqa: ARG002
        ) -> dict[str, Any]:
            """Resolve provider-specific parameter conflicts before the upstream call."""
            return _resolve_thinking_reasoning_conflict(data)

        async def async_log_success_event(
            self,
            kwargs,
            response_obj,
            start_time,
            end_time,
        ) -> None:
            record = build_record(
                kwargs,
                response_obj,
                status="success",
                start_time=start_time,
                end_time=end_time,
            )
            await _write_record_async(record, kwargs)
            _write_metadata(record, kwargs)

        async def async_log_failure_event(
            self,
            kwargs,
            response_obj,
            start_time,
            end_time,
        ) -> None:
            record = build_record(
                kwargs,
                response_obj,
                status="failure",
                exc=kwargs.get("exception"),
                start_time=start_time,
                end_time=end_time,
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
