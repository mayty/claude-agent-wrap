# This file has been created with the assistance of an AI tool.
"""Record normalization for the logs viewer."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agent_wrap.domain.logs.constants import ALIAS_NAME_RE, TITLE_RE
from agent_wrap.domain.logs.hash_resolver import resolve_hashes
from agent_wrap.domain.logs.models import ExtractedFields, NormalizedRecordBase

if TYPE_CHECKING:
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.providers.models import LogRecord


def _extract_record_fields(
    rec: LogRecord,
) -> ExtractedFields:
    """Extract (data, agent_id, reply, usage, finish_reason) from one record."""
    psr = rec.get("request")
    data: dict[str, Any] = {}
    agent_id: str | None = None
    if isinstance(psr, dict):
        body = psr.get("body")
        if isinstance(body, dict):
            data = body["data"] if isinstance(body.get("data"), dict) else body
        headers = psr.get("headers")
        if isinstance(headers, dict):
            hdr_id = headers.get("x-claude-code-agent-id")
            if isinstance(hdr_id, str) and hdr_id:
                agent_id = hdr_id

    response = rec.get("response")
    reply: dict[str, Any] = {}
    usage: dict[str, Any] = {}
    # A sibling of choices[0]["message"], not a child of it — which is why it was
    # dropped here before, leaving a filtered or truncated reply looking like an
    # ordinary one in the viewer.
    finish_reason: str | None = None
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                if isinstance(first.get("message"), dict):
                    reply = first["message"]
                raw_reason = first.get("finish_reason")
                if isinstance(raw_reason, str):
                    finish_reason = raw_reason
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return ExtractedFields(data, agent_id, reply, usage, finish_reason)


def normalize_record_unresolved(rec: LogRecord) -> NormalizedRecordBase:
    """
    Reduce one raw log record to the shape the UI consumes, WITHOUT resolving
    ``hash:<sha256>`` pointers.  Callers that want hash resolution should use
    :func:`normalize_record` instead.

    Pure (no I/O) so it can be unit-tested directly.
    """
    data, agent_id, reply, usage, finish_reason = _extract_record_fields(rec)
    raw_max_tokens = data.get("max_tokens")
    # bool is an int subclass, and a stray True here would render as a cap of 1.
    max_tokens = (
        raw_max_tokens
        if isinstance(raw_max_tokens, int) and not isinstance(raw_max_tokens, bool)
        else None
    )

    return {
        "timing": rec.get("timing"),
        "status": rec.get("status"),
        "model": rec.get("model"),
        "agent_id": agent_id,
        "messages": data.get("messages") or [],
        "system": data.get("system"),
        "tools": data.get("tools") or [],
        "response": reply,
        "usage": usage,
        "error": rec.get("error"),
        "finish_reason": finish_reason,
        "max_tokens": max_tokens,
    }


def normalize_record(rec: LogRecord, strings: dict[str, str]) -> NormalizedRecordBase:
    """
    Reduce one raw log record to the shape the UI consumes.

    Pure (no I/O) so it can be unit-tested directly. Pulls the real prompt
    from ``request.body.data`` and the reply from
    ``response.choices[0].message``, resolving ``hash:<sha256>`` pointers.
    """
    resolved = resolve_hashes(rec, strings)
    return normalize_record_unresolved(resolved)


def enrich_with_costs(
    normalized: NormalizedRecordBase,
    raw_response: dict[str, Any] | None,
    provider: str,
    pricing: PricingService,
    raw_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compute cost, cache pct, and token counts for one normalized record.

    Returns a dict of extra fields to merge into the record. Pure aside from
    the *pricing* service lookup, which is in-memory after the first fetch per
    provider.
    """
    model = normalized["model"] or ""
    usage = normalized["usage"] or {}

    # The request's cache_control TTL attributes cache writes to a 5m/1h tier
    # when the response omits the split (the Bedrock case).
    request_ttl = pricing.request_cache_ttl(raw_request)

    # Use the canonical token extraction so field-resolution logic lives in one
    # place (extract_usage handles prompt_tokens/input_tokens fallback, etc.).
    norm_usage = pricing.extract_usage(raw_response, request_ttl)
    in_t = norm_usage["input_tokens"]
    out_t = norm_usage["output_tokens"]
    cr_t = norm_usage["cache_read_input_tokens"]

    # Only set cache_percent when there are actual cache reads, so the frontend
    # can skip displaying "(0% cached)".
    cache_percent = None
    if in_t and cr_t:
        cache_percent = int(100 * cr_t / in_t)

    # Compute cost in USD when pricing data is available.
    cost = None
    if normalized["status"] == "success" and usage and model:
        cost = pricing.compute_cost(provider, model, usage=norm_usage)

    return {
        "context_tokens": in_t,
        "output_tokens": out_t,
        "cache_percent": cache_percent,
        "cost": cost,
    }


def _response_content_str(response: Any) -> str | None:
    """Pull the assistant's text from a JSON-safe response dict, or None."""
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


def extract_alias(rec: LogRecord) -> str | None:
    """
    Return Claude Code's kebab-case session name if ``rec`` is its naming call.

    Mirrors the callback's ``extract_session_alias`` so existing logs (written
    before the callback learned to persist metadata) still surface a
    name. Claude Code's naming response content is ``{"name": "<kebab-slug>"}``;
    the sibling ``{"title": ...}`` call is ignored. The slug is short and never
    hashed, so the raw record's response is read directly.
    """
    content = _response_content_str(rec["response"])
    if not content:
        return None
    stripped = content.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        match = ALIAS_NAME_RE.search(stripped)
        return match.group(1).strip() or None if match else None
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def extract_title(rec: LogRecord) -> str | None:
    """
    Return Claude Code's sentence-case session title if ``rec`` is its title-
    generation call (a short ``{"title": "…"}`` response, typically the first
    record of every session).
    """
    content = _response_content_str(rec["response"])
    if not content:
        return None
    stripped = content.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        match = TITLE_RE.search(stripped)
        return match.group(1).strip() or None if match else None
    if isinstance(obj, dict):
        title = obj.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None
