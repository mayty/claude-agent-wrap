# This file has been created with the assistance of an AI tool.
"""
LiteLLM custom callback that logs every LLM call to a JSONL file.

LiteLLM resolves the callback module relative to the config file's directory, so
this file must sit beside ``config.yaml`` (both at ``/etc/litellm/`` in the
sidecar). Logging failures are swallowed so they can never break the proxy.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_wrap.domain.providers.models import LogRecord

# Mounted into the sidecar at /etc/litellm/ alongside helpers.py and
# string_hasher.py, not as a package, so the plain imports below need this
# directory on sys.path.
_current_dir = str(Path(__file__).parent.resolve())
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)
from helpers import (  # noqa: E402  # pyrefly: ignore [missing-import]
    get_session_hasher,
    json_safe,
)
from string_hasher import StringHasher  # noqa: E402  # pyrefly: ignore [missing-import]

# One shared sidecar serves every project on the host, so its log directory is
# project-independent and each record is routed to
# /var/log/agent-wrap/<project_hash>/<provider>/<session_id>/. Only <provider> is
# fixed per sidecar (AGENT_WRAP_PROVIDER); the other two arrive per request, in
# the x-agent-wrap-log-prefix and x-claude-code-session-id headers.

# Lowercase hex is what project_path_hash emits, and matching that alphabet
# inherently rejects '/', '.' and '..' — so there is no separate traversal check.
_HASH_RE = re.compile(r"^[0-9a-f]+$")
_PROVIDER_RE = re.compile(r"^[a-z0-9-]+$")
_DEFAULT_PROJECT_HASH = "unknown-project"
_DEFAULT_PROVIDER = "unknown-provider"

# Where the incoming request headers can be found, most authoritative first.
#
# The two failure paths reach this module with differently-shaped dicts, and both
# must agree on which project/session directory a record belongs to — a record
# filed under "unknown-project" is written to disk but invisible in the viewer,
# which is the exact failure mode this callback exists to prevent.
#
#   - post_call_failure_hook gets the proxy's request_payload, which copies every
#     key of LiteLLM's kwargs (so "litellm_params" is present).
#   - log_failure_event gets the logging object's model_call_details, whose
#     "litellm_params" is the *same dict object* the passthrough handler built
#     (it is passed straight to Logging.update_environment_variables).
#
# The first path below therefore covers both today; the rest are cheap insurance
# against LiteLLM moving the headers, since the symptom would otherwise be silent.
# Failure ids already written, so the two failure hooks cannot double-record the
# same upstream error. Bounded so a long-lived sidecar cannot grow them without
# limit; see _claim_failure for why this is module-level rather than instance state.
_SEEN_FAILURE_LIMIT = 512
_SEEN_FAILURE_IDS: set[str] = set()
_SEEN_FAILURE_ORDER: deque[str] = deque(maxlen=_SEEN_FAILURE_LIMIT)

_HEADER_PATHS: tuple[tuple[str, ...], ...] = (
    ("litellm_params", "proxy_server_request", "headers"),
    ("proxy_server_request", "headers"),
    ("litellm_params", "metadata", "headers"),
)


def _dig(root: Any, path: tuple[str, ...]) -> Any:
    """Walk *path* through nested mappings, returning None if any hop is missing."""
    node = root
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _get_request_headers(kwargs: dict[str, Any]) -> Mapping[str, Any]:
    """
    Return the incoming HTTP headers, whichever shape LiteLLM handed us.

    Starlette's ``Headers`` is a ``Mapping`` with case-insensitive lookup, and the
    passthrough route stores that object directly rather than a dict copy — so the
    result is used only via ``.get()`` and never mutated.
    """
    for path in _HEADER_PATHS:
        headers = _dig(kwargs, path)
        if isinstance(headers, Mapping):
            return headers
    return {}


def _get_session_id(kwargs: dict[str, Any]) -> str:
    return _get_request_headers(kwargs).get("x-claude-code-session-id", "unknown-session")


def _get_project_hash(kwargs: dict[str, Any]) -> str:
    """
    Extract the project hash from the x-agent-wrap-log-prefix request header.

    Anything that is not pure lowercase hex falls back to a fixed default, so a
    malformed value can never escape the mount.
    """
    raw = _get_request_headers(kwargs).get("x-agent-wrap-log-prefix", "")
    return raw if isinstance(raw, str) and _HASH_RE.match(raw) else _DEFAULT_PROJECT_HASH


def _get_provider() -> str:
    """
    Return the provider name from the AGENT_WRAP_PROVIDER sidecar env var.

    Fixed for the shared sidecar's lifetime.
    """
    name = os.environ.get("AGENT_WRAP_PROVIDER", "")
    return name if _PROVIDER_RE.match(name) else _DEFAULT_PROVIDER


def _usage_from_slo(logging_object: dict[str, Any]) -> dict[str, Any] | None:
    """
    Synthesize a usage dict from LiteLLM's standard_logging_object token fields.

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
    # in a usage block when the SLO response lacks one. Replacing the whole dict would
    # drop the choices/message content the viewer renders and the ingester reads a
    # session's alias and title out of.
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


def build_record(  # noqa: PLR0913, PLR0917
    kwargs: dict[str, Any],
    response_obj: Any,
    status: str,
    exc: Any = None,
    start_time: Any = None,
    end_time: Any = None,
) -> LogRecord:
    """
    Build a JSON-serializable log record from a LiteLLM callback's arguments.

    The ``request`` field is the proxy server request itself; the real Anthropic
    request lives at ``request.body.data``.
    """
    session_id = _get_session_id(kwargs)
    log_dir = _get_log_dir(kwargs)
    hasher = get_session_hasher(session_id, log_dir)

    litellm_params = kwargs.get("litellm_params") or {}
    psr = litellm_params.get("proxy_server_request")

    # The only cycle in LiteLLM's structure: body holds a "proxy_server_request"
    # key pointing back at its parent. Dropping it leaves a DAG json.dumps accepts.
    if isinstance(psr, dict) and isinstance(psr.get("body"), dict):
        psr["body"].pop("proxy_server_request", None)

    logging_object = kwargs.get("standard_logging_object", {})
    model = kwargs.get("model")
    # Failures carry no usage to lose; successes must retain it for cost accounting.
    if status == "success":
        response = _usable_response(response_obj, logging_object, hasher)
    else:
        response = json_safe(response_obj, hasher)
    record: LogRecord = {
        "timing": {
            "start": _epoch(logging_object.get("startTime"), start_time),
            # No datetime fallback on a failure: the call produced no first token, so
            # inheriting the call's start would report a 0s time-to-first-token in the
            # viewer instead of omitting the figure. A real streamed-then-errored call
            # still keeps whatever completionStartTime the logging object recorded.
            "completionStart": _epoch(
                logging_object.get("completionStartTime"),
                start_time if status == "success" else None,
            ),
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

    hasher.flush(log_dir)

    return record


def _get_log_dir(kwargs: dict[str, Any]) -> Path:
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

    hasher = get_session_hasher(session_id, log_dir)
    await asyncio.to_thread(hasher.flush, log_dir)

    log_file = log_dir / "messages.jsonl"
    line = json.dumps(record, default=str)

    def _append() -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    await asyncio.to_thread(_append)


def _claim_failure(call_id: Any) -> bool:
    """
    Return True if this failure is ours to record, False if a sibling hook has it.

    A single upstream error can reach this module twice — once through
    ``async_post_call_failure_hook`` (the proxy's passthrough handler) and once
    through ``async_log_failure_event`` (the ``_async_failure_callback`` list) —
    and ``litellm_call_id`` is the one identifier both paths carry.

    The bookkeeping is module-level rather than instance state on purpose:
    LiteLLM's ``get_instance_fn`` re-execs this file per load and mints a distinct
    ``FileLogger`` each time (see the registration note at the bottom of this
    file), so an instance attribute would not be shared between them.

    A missing or non-string id records anyway: a duplicate row beats another
    silently dropped request.

    No lock: callbacks run on the proxy's event loop and there is no await between
    the membership test and the insert, so check-then-set is atomic here.
    """
    if not isinstance(call_id, str) or not call_id:
        return True
    if call_id in _SEEN_FAILURE_IDS:
        return False
    # deque(maxlen=...) evicts silently on append; drop the same id from the set
    # so the two structures cannot drift apart over a long-lived sidecar.
    if len(_SEEN_FAILURE_ORDER) == _SEEN_FAILURE_ORDER.maxlen:
        _SEEN_FAILURE_IDS.discard(_SEEN_FAILURE_ORDER[0])
    _SEEN_FAILURE_ORDER.append(call_id)
    _SEEN_FAILURE_IDS.add(call_id)
    return True


async def _record_failure(
    kwargs: dict[str, Any],
    response_obj: Any = None,
    exc: Any = None,
    start_time: Any = None,
    end_time: Any = None,
) -> None:
    """
    Build and persist one failure record.  Never raises.

    Falls back to "now" for timing bounds a caller did not supply.
    ``async_post_call_failure_hook`` is handed no ``datetime`` bounds, and on the
    ``/anthropic/*`` passthrough route the ``standard_logging_object`` carries no
    timestamps either — so without this the record lands with an all-null
    ``timing`` and drops out of the viewer's chronological order. A real timestamp
    still wins: ``_epoch`` consults these bounds only when the SLO has none.
    """
    if not _claim_failure(kwargs.get("litellm_call_id")):
        return
    now = datetime.now(tz=UTC)
    record = build_record(
        kwargs,
        response_obj,
        status="failure",
        exc=exc,
        start_time=start_time or now,
        end_time=end_time or now,
    )
    try:
        await _write_record_async(record, kwargs)
    except Exception as e:  # noqa: BLE001 - logging is best-effort
        print(f"agent-wrap callback: failed to write log record: {e}", file=sys.stderr)


def _resolve_thinking_reasoning_conflict(data: dict[str, Any]) -> dict[str, Any]:
    """
    Resolve API conflict between ``thinking`` and ``reasoning_effort``.

    DeepSeek's Anthropic-compatible API rejects a request carrying both
    ``thinking.type == "disabled"`` and a reasoning effort — a combination Claude
    Code sends for lightweight calls when ``CLAUDE_CODE_EFFORT_LEVEL`` is set.
    """
    if not isinstance(data.get("thinking"), dict) or data["thinking"].get("type") != "disabled":
        return data

    if not isinstance(data.get("output_config"), dict):
        return data

    data["output_config"].pop("effort", None)

    return data


try:
    # litellm is only installed inside the sidecar container, not the dev env.
    import litellm  # pyrefly: ignore[missing-import]
    from litellm.integrations.custom_logger import CustomLogger  # pyrefly: ignore[missing-import]

    class FileLogger(CustomLogger):
        """LiteLLM CustomLogger that appends each call to the JSONL log file asynchronously."""

        async def async_pre_call_hook(
            self,
            user_api_key_dict,  # noqa: ARG002  # pyrefly: ignore [implicit-any-parameter]
            cache,  # noqa: ARG002  # pyrefly: ignore [implicit-any-parameter]
            data: dict[str, Any],
            call_type: str,  # noqa: ARG002
        ) -> dict[str, Any]:
            """
            Resolve provider-specific parameter conflicts before the upstream call.

            Not called on LiteLLM's /anthropic/* passthrough route, which the
            litellm-anthropic-sub provider uses. That is harmless rather than an
            oversight: the conflict resolved here is rejected by DeepSeek's
            Anthropic-compatible API, not by Anthropic's own.
            """
            return _resolve_thinking_reasoning_conflict(data)

        async def async_post_call_failure_hook(
            self,
            request_data: dict[str, Any],
            original_exception: Exception,
            user_api_key_dict,  # noqa: ARG002  # pyrefly: ignore [implicit-any-parameter]
            traceback_str: str | None = None,  # noqa: ARG002
        ) -> None:
            """
            Record failures raised on LiteLLM's passthrough routes.

            The router-based routes report failures via ``async_log_failure_event``,
            but a non-2xx on a passthrough route (``/anthropic/*``) raises an
            HTTPException that LiteLLM surfaces through ``post_call_failure_hook``
            instead. Without this hook, upstream errors on that route would be
            absent from ``messages.jsonl`` entirely — silently, which is the worst
            way for a request log to be wrong.

            Not sufficient alone: measured against the on-disk logs it catches
            non-streaming failures only, and streaming is all of Claude Code's
            conversation traffic. ``async_log_failure_event`` covers the rest;
            ``_claim_failure`` keeps the overlap from double-recording.
            """
            await _record_failure(request_data, exc=original_exception)

        async def async_log_success_event(
            self,
            kwargs,  # pyrefly: ignore [implicit-any-parameter]
            response_obj,  # pyrefly: ignore [implicit-any-parameter]
            start_time,  # pyrefly: ignore [implicit-any-parameter]
            end_time,  # pyrefly: ignore [implicit-any-parameter]
        ) -> None:
            record = build_record(
                kwargs,
                response_obj,
                status="success",
                start_time=start_time,
                end_time=end_time,
            )
            await _write_record_async(record, kwargs)

        async def async_log_stream_event(
            self,
            kwargs,  # pyrefly: ignore [implicit-any-parameter]
            response_obj,  # pyrefly: ignore [implicit-any-parameter]
            start_time,  # pyrefly: ignore [implicit-any-parameter]
            end_time,  # pyrefly: ignore [implicit-any-parameter]
        ) -> None:
            """
            Record a streaming call that LiteLLM routed away from the success hook.

            LiteLLM's CustomLogger dispatch calls this instead of
            ``async_log_success_event`` when ``stream`` is true and
            ``model_call_details`` has no ``async_complete_streaming_response``. The
            base-class implementation is a bare ``pass``, so any request taking that
            branch would be dropped from the log without a trace.

            Both routes we use populate that key before dispatching, so this is a
            safety net rather than dead code — the alternative fails silently.
            """
            await self.async_log_success_event(kwargs, response_obj, start_time, end_time)

        async def async_log_failure_event(
            self,
            kwargs,  # pyrefly: ignore [implicit-any-parameter]
            response_obj,  # pyrefly: ignore [implicit-any-parameter]
            start_time,  # pyrefly: ignore [implicit-any-parameter]
            end_time,  # pyrefly: ignore [implicit-any-parameter]
        ) -> None:
            """
            Record a failure dispatched through ``litellm._async_failure_callback``.

            Reached on the router routes, and — since the registration at the
            bottom of this file — on the ``/anthropic/*`` passthrough route too,
            where it is what finally captures streaming upstream errors such as
            Anthropic's 529 ``overloaded_error``.
            """
            await _record_failure(
                kwargs,
                response_obj,
                exc=kwargs.get("exception"),
                start_time=start_time,
                end_time=end_time,
            )

    file_logger_instance = FileLogger()

    # Registered here rather than from config.yaml because no config key reaches
    # litellm._async_success_callback, which is the list async_success_handler
    # dispatches from. `callbacks:` populates litellm.callbacks (failures only);
    # `success_callback:` is gated on _is_async_callable, which a CustomLogger
    # instance fails. On the router path function_setup() copies callbacks across,
    # but the /anthropic/* passthrough route never calls it — so every success is
    # dropped. add_litellm_async_success_callback appends with no such gate.
    #
    # Safe on every load: get_instance_fn re-execs this module and mints a distinct
    # instance each time, but _add_custom_logger_to_list dedups by class name plus
    # public scalar attrs, and FileLogger is stateless.
    litellm.logging_callback_manager.add_litellm_async_success_callback(file_logger_instance)

    # Same treatment for failures, and for one more reason: a census of the on-disk
    # logs showed the config-driven path (post_call_failure_hook over
    # litellm.callbacks) firing only for non-streaming requests — thousands of
    # streamed successes against a single streamed failure. Every real conversation
    # turn streams, so 529 overloaded_error was missing from exactly the requests
    # that matter. async_failure_handler reads litellm._async_failure_callback,
    # which the passthrough route leaves empty for the same reason as the success
    # list. Both hooks feed _record_failure, which dedups on litellm_call_id.
    litellm.logging_callback_manager.add_litellm_async_failure_callback(file_logger_instance)
except ImportError:
    # litellm is only installed inside the sidecar. build_record stays importable.
    file_logger_instance = None
