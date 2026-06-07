# This file has been created with the assistance of an AI tool.
"""
The `logs` subcommand — a local web viewer for the LiteLLM request logs.

Commit #14 made the shared sidecar append every upstream model call to
per-session JSONL files under each project's
``.claude/litellm-logs/<provider>/<session_id>/``. `agent stats` aggregates
those into token/cost totals; this command instead lets you *read* the raw
exchanges in a browser: pick a project, pick a session, and see each logged
request rendered chat-style (system prompt, the message thread with
tool_use/tool_result blocks, the response, and token usage).

Everything is Python stdlib only (``http.server``) — no extra dependency, no
``agent rebuild``, no Docker. It runs on the host exactly like `agent stats`.
The web UI is a small set of static assets (``index.html``/``app.js``/
``styles.css``) under the repo-root ``logs_page/`` directory, served on demand
by a minimal static file server.

The data model (confirmed against real logs):

* ``messages.jsonl`` — one record per call. The *real* Anthropic request lives
  at ``request.proxy_server_request.body.data`` (``messages``/``system``/
  ``tools``); the top-level ``request.messages`` is a LiteLLM placeholder
  (``"default-message-value"``) and is ignored. The reply is OpenAI-shaped at
  ``response.choices[0].message`` with ``response.usage`` token counts.
* ``strings.jsonl`` — ``{"hash": "hash:<sha256>", "original": ...}`` lines.
  Long strings in the record are replaced by ``hash:<sha256>`` pointers; we load
  this map and resolve them for display.
* Records may carry ``wrap-ref:<id>`` cycle markers and ``wrap-ref-id`` keys
  from the callback's circular-reference handling; those are resolved here.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_wrap.commands.stats import PriceSource, extract_usage
from agent_wrap.lib.usage_args import load_projects

USAGE = "[--port N]"
SUMMARY = "Browse LiteLLM request logs in a local web viewer"

# The web UI ships as static assets under the repo-root ``logs_page/`` dir
# (logs.py is at <root>/agent_wrap/commands/, so the root is parents[2]).
_LOGS_PAGE_DIR = Path(__file__).resolve().parents[2] / "logs_page"

# Extension -> Content-Type for the static file server. Anything else is served
# as a generic binary download.
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

_DEFAULT_PORT = 8765
_PORT_SCAN_LIMIT = 50
_MIN_PORT = 1
_MAX_PORT = 65535

_PRICES = PriceSource()

# ---------------------------------------------------------------------------
# Hash resolution
# ---------------------------------------------------------------------------


def load_strings(session_dir: Path) -> dict[str, str]:
    """Load a session's ``strings.jsonl`` into a ``{hash: original}`` map."""
    strings: dict[str, str] = {}
    strings_file = session_dir / "strings.jsonl"
    if not strings_file.is_file():
        return strings
    try:
        with strings_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    entry = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                h = entry.get("hash")
                if isinstance(h, str) and "original" in entry:
                    strings[h] = entry["original"]
    except OSError:
        pass
    return strings


_WRAP_REF_PREFIX = "wrap-ref:"
_WRAP_REF_PREFIX_LEN = len(_WRAP_REF_PREFIX)
_WRAP_REF_ID_PREFIX = "wrap-ref-id:"


def _index_refs(o: Any, wrap_ref_index: dict[str, Any], obj_to_ref: dict[int, str]) -> None:
    """Pass 1: Collect wrap-ref-id markers into lookup indexes."""
    if isinstance(o, dict):
        if "wrap-ref-id" in o:
            ref_id = str(o.pop("wrap-ref-id"))
            wrap_ref_index[ref_id] = o
            obj_to_ref[id(o)] = ref_id
        for v in o.values():
            _index_refs(v, wrap_ref_index, obj_to_ref)
    elif isinstance(o, list):
        # wrap-ref-id is always inserted at index 0 by the callback.
        # Check index 0 directly to avoid modifying a list while iterating.
        if o and isinstance(o[0], str) and o[0].startswith(_WRAP_REF_ID_PREFIX):
            ref_id = o[0][len(_WRAP_REF_ID_PREFIX) :]
            wrap_ref_index[ref_id] = o
            obj_to_ref[id(o)] = ref_id
            o.pop(0)
        for item in o:
            _index_refs(item, wrap_ref_index, obj_to_ref)


class _RefResolver:
    """Helper class to resolve wrap-refs and hashes while safely handling cycles."""

    def __init__(
        self,
        wrap_ref_index: dict[str, Any],
        obj_to_ref: dict[int, str],
        strings: dict[str, str],
    ):
        self.wrap_ref_index = wrap_ref_index
        self.obj_to_ref = obj_to_ref
        self.strings = strings
        self.resolved_canons: dict[str, Any] = {}

    def resolve(self, o: Any) -> Any:
        if isinstance(o, str):
            return self._resolve_str(o)

        obj_id = id(o)
        if obj_id in self.obj_to_ref:
            ref_id = self.obj_to_ref[obj_id]
            if ref_id in self.resolved_canons:
                return self.resolved_canons[ref_id]

            # Create a placeholder to break cycles and cache it immediately.
            # Then populate it by resolving its children.
            placeholder: Any = {} if isinstance(o, dict) else []
            self.resolved_canons[ref_id] = placeholder

            if isinstance(o, dict):
                for k, v in o.items():
                    placeholder[k] = self.resolve(v)
            else:
                for v in o:
                    placeholder.append(self.resolve(v))
            return placeholder

        if isinstance(o, dict):
            return {k: self.resolve(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.resolve(v) for v in o]
        return o

    def _resolve_str(self, o: str) -> Any:
        if not o.startswith(_WRAP_REF_PREFIX) or len(o) <= _WRAP_REF_PREFIX_LEN:
            return self.strings.get(o, o)

        ref_id = o[_WRAP_REF_PREFIX_LEN:]
        if ref_id in self.resolved_canons:
            return self.resolved_canons[ref_id]

        canon = self.wrap_ref_index.get(ref_id)
        if canon is None:
            return o

        # Create a placeholder to break cycles and cache it immediately.
        # Then populate it by resolving its children.
        placeholder: Any = {} if isinstance(canon, dict) else []
        self.resolved_canons[ref_id] = placeholder

        if isinstance(canon, dict):
            for k, v in canon.items():
                placeholder[k] = self.resolve(v)
        else:
            for v in canon:
                placeholder.append(self.resolve(v))

        return placeholder


def resolve(obj: Any, strings: dict[str, str]) -> Any:
    """
    Recursively replace ``hash:<sha256>`` strings with their originals and
    reconstruct the callback's circular-reference bookkeeping
    (``wrap-ref:<id>`` -> canonical object with ``wrap-ref-id``).

    Unknown hashes are left intact so a missing ``strings.jsonl`` entry is
    visible rather than silently blanked. Uses a placeholder cache to safely
    resolve hashes inside canonical objects and break circular reference cycles.
    """
    wrap_ref_index: dict[str, Any] = {}
    obj_to_ref: dict[int, str] = {}
    _index_refs(obj, wrap_ref_index, obj_to_ref)
    return _RefResolver(wrap_ref_index, obj_to_ref, strings).resolve(obj)


# ---------------------------------------------------------------------------
# Record normalization
# ---------------------------------------------------------------------------


def normalize_record(rec: dict, strings: dict[str, str]) -> dict[str, Any]:
    """
    Reduce one raw log record to the shape the UI consumes.

    Pure (no I/O) so it can be unit-tested directly. Pulls the real prompt from
    ``request.proxy_server_request.body.data`` and the reply from
    ``response.choices[0].message``, resolving hashes and wrap-refs throughout.

    NOTE: Resolution MUST happen on the full raw record *before* extracting fields,
    because canonical wrap-ref targets might live in parts of the record that are
    later discarded (e.g., the top-level LiteLLM placeholder messages).
    """
    # Resolve the entire raw record first so all wrap-ref targets are indexed,
    # even if they live in fields we will subsequently ignore.
    resolved_rec = resolve(rec, strings)

    request = resolved_rec.get("request") or {}
    psr = request.get("proxy_server_request")
    data: dict[str, Any] = {}
    agent_id: str | None = None
    if isinstance(psr, dict):
        body = psr.get("body")
        if isinstance(body, dict):
            data = body["data"] if isinstance(body.get("data"), dict) else body
        # Subagent turns carry an ``x-claude-code-agent-id`` request header; the
        # main loop's requests have none. The id is short and unhashed, so it is
        # read straight from the headers dict. It groups a subagent's turns in
        # the viewer, which otherwise interleaves them with the main thread.
        headers = psr.get("headers")
        if isinstance(headers, dict):
            hdr_id = headers.get("x-claude-code-agent-id")
            if isinstance(hdr_id, str) and hdr_id:
                agent_id = hdr_id

    response = resolved_rec.get("response")
    reply: dict[str, Any] = {}
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict) and isinstance(first.get("message"), dict):
                reply = first["message"]
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    else:
        usage = {}

    return {
        "ts": resolved_rec.get("ts"),
        "status": resolved_rec.get("status"),
        "model": resolved_rec.get("model"),
        "agent_id": agent_id,
        "messages": data.get("messages") or [],
        "system": data.get("system"),
        "tools": data.get("tools") or [],
        "response": reply,
        "usage": usage,
        "error": resolved_rec.get("error"),
    }


def _enrich_with_costs(
    normalized: dict, raw_response: dict | None, provider: str
) -> dict[str, Any]:
    """
    Compute cost, cache pct, and token counts for one normalized record.

    Returns a dict of extra fields to merge into the record. Pure aside from
    the ``_PRICES`` lookup, which is in-memory after the first fetch per provider.
    """
    model = normalized.get("model") or ""
    usage = normalized.get("usage") or {}

    # Use the canonical token extraction so field-resolution logic lives in one
    # place (extract_usage handles prompt_tokens/input_tokens fallback, etc.).
    norm_usage = extract_usage(raw_response)
    in_t = norm_usage.get("input_tokens", 0)
    out_t = norm_usage.get("output_tokens", 0)
    cr_t = norm_usage.get("cache_read_input_tokens", 0)

    # Only set cache_percent when there are actual cache reads, so the frontend
    # can skip displaying "(0% cached)".
    cache_percent = None
    if in_t and cr_t:
        cache_percent = int(100 * cr_t / in_t)

    # Compute cost in USD when pricing data is available.
    cost = None
    if normalized.get("status") == "success" and usage and model:
        cost = _PRICES.compute_cost(provider, model, raw_response)

    return {
        "context_tokens": in_t,
        "output_tokens": out_t,
        "cache_percent": cache_percent,
        "cost": cost,
    }


_ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')
_TITLE_RE = re.compile(r'"title"\s*:\s*"([^"]+)"')


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


def extract_alias(rec: dict) -> str | None:
    """
    Return Claude Code's kebab-case session name if ``rec`` is its naming call.

    Mirrors the callback's ``extract_session_alias`` so existing logs (written
    before the callback learned to persist an ``alias`` file) still surface a
    name. Claude Code's naming response content is ``{"name": "<kebab-slug>"}``;
    the sibling ``{"title": ...}`` call is ignored. The slug is short and never
    hashed, so the raw record's response is read directly.
    """
    content = _response_content_str(rec.get("response"))
    if not content:
        return None
    stripped = content.strip()
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        match = _ALIAS_NAME_RE.search(stripped)
        return match.group(1).strip() or None if match else None
    if isinstance(obj, dict):
        name = obj.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def extract_title(rec: dict) -> str | None:
    """
    Return Claude Code's sentence-case session title if ``rec`` is its title-
    generation call (a short ``{"title": "…"}`` response, typically the first
    record of every session).
    """
    content = _response_content_str(rec.get("response"))
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


# ---------------------------------------------------------------------------
# Filesystem access
# ---------------------------------------------------------------------------


def _logs_dir(project: Path) -> Path:
    return project / ".claude" / "litellm-logs"


def list_projects(tool_dir: Path) -> list[dict[str, Any]]:
    """List registered projects that have a LiteLLM logs directory."""
    registry = tool_dir / ".agent-launches" / "projects.txt"
    if not registry.is_file():
        return []

    out: list[dict[str, Any]] = []
    for idx, path in enumerate(load_projects(registry)):
        if not _logs_dir(path).is_dir():
            continue
        sessions = list_sessions(path)
        last_ts = max((s["last_ts"] for s in sessions if s["last_ts"]), default=None)
        out.append(
            {
                "id": idx,
                "path": str(path),
                "name": path.name,
                "sessions": len(sessions),
                "last_ts": last_ts,
            }
        )
    out.sort(key=lambda p: p["last_ts"] or "", reverse=True)
    return out


def _project_by_id(tool_dir: Path, project_id: int) -> Path | None:
    registry = tool_dir / ".agent-launches" / "projects.txt"
    if not registry.is_file():
        return None
    projects = load_projects(registry)
    if 0 <= project_id < len(projects):
        return projects[project_id]
    return None


class _SessionMeta:
    """Accumulates cheap per-session metadata as records are scanned."""

    def __init__(self) -> None:
        self.count = 0
        self.first_ts: str | None = None
        self.last_ts: str | None = None
        self.models: set[str] = set()
        self.derived_alias: str | None = None
        self.derived_title: str | None = None

    def add(self, rec: dict) -> None:
        self.count += 1
        ts = rec.get("ts")
        if isinstance(ts, str):
            if self.first_ts is None:
                self.first_ts = ts
            self.last_ts = ts
        model = rec.get("model")
        if isinstance(model, str):
            self.models.add(model.rsplit("/", 1)[-1])
        alias = extract_alias(rec)
        if alias:
            self.derived_alias = alias
        title = extract_title(rec)
        if title:
            self.derived_title = title


def _scan_session_meta(session_dir: Path, provider: str) -> dict[str, Any] | None:
    """Cheap metadata for one session: count, first/last ts, models, alias."""
    messages_file = session_dir / "messages.jsonl"
    if not messages_file.is_file():
        return None

    meta = _SessionMeta()
    try:
        with messages_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                meta.add(rec)
    except OSError:
        return None

    if meta.count == 0:
        return None

    return {
        "provider": provider,
        "session_id": session_dir.name,
        # An explicit `alias` file (callback-written) wins over derivation.
        "alias": _read_alias_file(session_dir) or meta.derived_alias,
        "title": meta.derived_title,
        "count": meta.count,
        "first_ts": meta.first_ts,
        "last_ts": meta.last_ts,
        "models": sorted(meta.models),
    }


def _read_alias_file(session_dir: Path) -> str | None:
    """Return the alias persisted beside the logs, if the callback wrote one."""
    alias_file = session_dir / "alias"
    if not alias_file.is_file():
        return None
    try:
        text = alias_file.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    return text or None


def _merge_session_meta(existing: dict[str, Any], meta: dict[str, Any]) -> None:
    """Merge *meta* (from one provider) into *existing* (the combined entry)."""
    existing["providers"].append(meta["provider"])
    existing["providers"].sort()
    existing["count"] += meta["count"]
    if meta["first_ts"] and (not existing["first_ts"] or meta["first_ts"] < existing["first_ts"]):
        existing["first_ts"] = meta["first_ts"]
    if meta["last_ts"] and (not existing["last_ts"] or meta["last_ts"] > existing["last_ts"]):
        existing["last_ts"] = meta["last_ts"]
    existing["models"] = sorted(set(existing["models"]) | set(meta["models"]))
    if existing["alias"] is None and meta["alias"] is not None:
        existing["alias"] = meta["alias"]
    if existing.get("title") is None and meta.get("title") is not None:
        existing["title"] = meta["title"]


def list_sessions(project: Path) -> list[dict[str, Any]]:
    """
    List sessions (newest first) across every provider in a project.

    Sessions with the same ``session_id`` across different providers are merged
    into a single entry so a mid-session provider switch doesn't produce
    duplicate rows in the viewer.
    """
    logs_dir = _logs_dir(project)
    if not logs_dir.is_dir():
        return []

    by_session: dict[str, dict[str, Any]] = {}
    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        for session_dir in provider_dir.iterdir():
            if not session_dir.is_dir():
                continue
            meta = _scan_session_meta(session_dir, provider_dir.name)
            if meta is None:
                continue
            sid = meta["session_id"]
            if sid in by_session:
                _merge_session_meta(by_session[sid], meta)
            else:
                meta["providers"] = [meta.pop("provider")]
                by_session[sid] = meta

    out = list(by_session.values())
    out.sort(key=lambda s: s["last_ts"] or "", reverse=True)
    return out


def session_fingerprint(project: Path, session_id: str) -> dict[str, Any]:
    """
    Return a combined change-marker for a session across all providers.

    Returns ``{"mtime": max_mtime_ns, "size": sum_sizes}`` across every
    provider directory that holds this session, so a new record from any
    provider triggers a refresh in the polling loop.  Returns
    ``{"mtime": None, "size": None}`` when no provider has the session.
    """
    logs_dir = _logs_dir(project)
    if not logs_dir.is_dir():
        return {"mtime": None, "size": None}

    best_mtime: int | None = None
    total_size: int | None = None
    found = False

    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        messages_file = provider_dir / session_id / "messages.jsonl"
        try:
            st = messages_file.stat()
        except OSError:
            continue
        found = True
        if best_mtime is None or st.st_mtime_ns > best_mtime:
            best_mtime = st.st_mtime_ns
        total_size = (total_size or 0) + st.st_size

    if not found:
        return {"mtime": None, "size": None}
    return {"mtime": best_mtime, "size": total_size}


def sessions_fingerprint(project: Path) -> dict[str, Any]:
    """
    Return a change-marker for all sessions in a project.

    Like :func:`session_fingerprint` but across every session directory so the
    frontend's sessions-list poll can detect new sessions, new records, and
    metadata changes without re-reading every messages.jsonl.

    Returns ``{"mtime": max_mtime_ns, "size": sum_sizes}`` across every
    ``messages.jsonl`` under the project's logs directory.  Returns
    ``{"mtime": None, "size": None}`` when no sessions exist.
    """
    logs_dir = _logs_dir(project)
    if not logs_dir.is_dir():
        return {"mtime": None, "size": None}

    best_mtime: int | None = None
    total_size: int | None = None
    found = False

    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        for session_dir in provider_dir.iterdir():
            if not session_dir.is_dir():
                continue
            messages_file = session_dir / "messages.jsonl"
            try:
                st = messages_file.stat()
            except OSError:
                continue
            found = True
            if best_mtime is None or st.st_mtime_ns > best_mtime:
                best_mtime = st.st_mtime_ns
            total_size = (total_size or 0) + st.st_size

    if not found:
        return {"mtime": None, "size": None}
    return {"mtime": best_mtime, "size": total_size}


def projects_fingerprint(tool_dir: Path) -> dict[str, Any]:
    """
    Return a change-marker for all registered projects that have logs.

    Includes the registry file's mtime so new project registrations and removals
    also change the fingerprint.  Returns ``{"mtime": max_mtime_ns, "size":
    sum_sizes}`` across every ``messages.jsonl`` under every project.

    Returns ``{"mtime": None, "size": None}`` when no projects have logs.
    """
    registry = tool_dir / ".agent-launches" / "projects.txt"
    best_mtime: int | None = None
    total_size: int | None = None

    # Include the registry itself so new/removed projects change the fingerprint.
    try:
        st = registry.stat()
        best_mtime = st.st_mtime_ns
        total_size = st.st_size
    except OSError:
        return {"mtime": None, "size": None}

    for project in load_projects(registry):
        fp = sessions_fingerprint(project)
        if fp["mtime"] is None:
            continue
        if best_mtime is None or fp["mtime"] > best_mtime:
            best_mtime = fp["mtime"]
        total_size = (total_size or 0) + (fp["size"] or 0)

    return {"mtime": best_mtime, "size": total_size}


def _read_provider_session(
    session_dir: Path, provider: str, session_id: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """
    Read and normalize records from one provider's session directory.

    Returns ``(records, meta_entry)`` where *meta_entry* has the same shape as
    :func:`_scan_session_meta` (or ``None`` when the directory has no records).
    """
    messages_file = session_dir / "messages.jsonl"
    if not messages_file.is_file():
        return [], None

    strings = load_strings(session_dir)
    meta = _SessionMeta()
    records: list[dict[str, Any]] = []
    try:
        with messages_file.open("r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                meta.add(rec)
                raw_response = rec.get("response")
                normalized = normalize_record(rec, strings)
                enriched = _enrich_with_costs(normalized, raw_response, provider)
                normalized.update(enriched)
                records.append(normalized)
    except OSError:
        pass

    if meta.count == 0:
        return [], None

    entry: dict[str, Any] = {
        "provider": provider,
        "session_id": session_id,
        "alias": _read_alias_file(session_dir) or meta.derived_alias,
        "title": meta.derived_title,
        "count": meta.count,
        "first_ts": meta.first_ts,
        "last_ts": meta.last_ts,
        "models": sorted(meta.models),
    }
    return records, entry


def read_session(project: Path, session_id: str) -> dict[str, Any]:
    """
    Read and normalize every request in one session across all providers.

    When a session spans multiple providers (e.g. the user switched mid-session),
    records from every provider directory are loaded and merge-sorted by ``ts``
    so the chat view shows a single chronological thread.

    Returns ``{"reqs": [...], "session_meta": {...}}`` where *session_meta* has
    the same shape as one entry from :func:`list_sessions` (or ``None`` when no
    records exist).
    """
    logs_dir = _logs_dir(project)
    if not logs_dir.is_dir():
        return {"reqs": [], "session_meta": None}

    all_records: list[dict[str, Any]] = []
    combined_meta: dict[str, Any] | None = None

    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        records, entry = _read_provider_session(
            provider_dir / session_id, provider_dir.name, session_id
        )
        all_records.extend(records)
        if entry is not None:
            if combined_meta is None:
                combined_meta = entry
                combined_meta["providers"] = [combined_meta.pop("provider")]
            else:
                _merge_session_meta(combined_meta, entry)

    all_records.sort(key=lambda r: r.get("ts") or "")
    return {"reqs": all_records, "session_meta": combined_meta}


# ---------------------------------------------------------------------------
# Static asset serving
# ---------------------------------------------------------------------------


def resolve_static(page_dir: Path, url_path: str) -> Path | None:
    """
    Map a URL path to a file inside ``page_dir``, or None if it escapes the dir.

    ``/`` maps to ``index.html``. Pure (the returned path may not exist); callers
    handle a missing file as a 404. Path traversal (``..``) and absolute targets
    are rejected by resolving and confirming containment within ``page_dir``.
    """
    rel = url_path.lstrip("/") or "index.html"
    base = page_dir.resolve()
    candidate = (base / rel).resolve()
    if candidate != base and base not in candidate.parents:
        return None
    return candidate


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Serves the static web UI and the read-only JSON API."""

    # Both bound as class attributes in serve().
    tool_dir: Path
    page_dir: Path

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Silence the default per-request stderr logging.
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/api/projects":
            self._send_json(list_projects(self.tool_dir))
            return

        if path == "/api/sessions":
            project = self._resolve_project(params)
            if project is None:
                self._send_json({"error": "unknown project"}, status=404)
            else:
                self._send_json(list_sessions(project))
            return

        if path == "/api/projects-stat":
            self._send_json(projects_fingerprint(self.tool_dir))
            return

        if path == "/api/sessions-stat":
            project = self._resolve_project(params)
            if project is None:
                self._send_json({"error": "unknown project"}, status=404)
            else:
                self._send_json(sessions_fingerprint(project))
            return

        # /api/session and /api/session-stat need a (project, session) pair;
        # resolve once and dispatch to the matching reader.
        if path in ("/api/session", "/api/session-stat"):
            pair = self._resolve_session(params)
            if pair is not None:
                reader = read_session if path == "/api/session" else session_fingerprint
                self._send_json(reader(*pair))
            return

        self._serve_static(path)

    def _resolve_session(self, params: dict[str, list[str]]) -> tuple[Path, str] | None:
        """Resolve a (project, session) pair; 400 + None if incomplete."""
        project = self._resolve_project(params)
        session = (params.get("session") or [""])[0]
        if project is None or not session:
            self._send_json({"error": "missing project/session"}, status=400)
            return None
        return project, session

    def _serve_static(self, url_path: str) -> None:
        """Serve a file from ``page_dir``; 404 on traversal or a missing file."""
        target = resolve_static(self.page_dir, url_path)
        if target is None or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        content_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, body, content_type)

    def _resolve_project(self, params: dict[str, list[str]]) -> Path | None:
        raw = (params.get("project") or [""])[0]
        try:
            project_id = int(raw)
        except ValueError:
            return None
        return _project_by_id(self.tool_dir, project_id)


def _bind(port: int) -> tuple[ThreadingHTTPServer, int] | None:
    """
    Bind on 127.0.0.1, scanning upward from ``port`` for a free one.

    Returns the (server, port) pair, or None when the whole scan range is busy.
    """
    for candidate in range(port, port + _PORT_SCAN_LIMIT):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), _Handler)
        except OSError:
            continue
        return server, candidate
    return None


def serve(tool_dir: Path, port: int) -> int:
    """Start the viewer and block until interrupted. Returns an exit code."""
    if not _LOGS_PAGE_DIR.is_dir():
        print(f"agent logs: cannot find UI assets at {_LOGS_PAGE_DIR}", file=sys.stderr)
        return 1

    # ThreadingHTTPServer instantiates the handler class per request, so bind
    # the shared state as class attributes the handler reads on each request.
    _Handler.tool_dir = tool_dir
    _Handler.page_dir = _LOGS_PAGE_DIR

    bound = _bind(port)
    if bound is None:
        print(
            f"agent logs: no free port in range {port}-{port + _PORT_SCAN_LIMIT - 1}",
            file=sys.stderr,
        )
        return 1
    server, actual_port = bound

    url = f"http://127.0.0.1:{actual_port}"
    print(f"agent logs: serving LiteLLM log viewer at {url}")
    print("Press Ctrl-C to stop.")
    # Best-effort: headless hosts have no browser.
    with contextlib.suppress(Exception):
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nagent logs: shutting down.")
    finally:
        server.server_close()
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE_TEXT = (
    "Usage: agent logs [--port N]\n\n"
    "Starts a local web viewer for the LiteLLM request logs written under each\n"
    "project's .claude/litellm-logs/ directory. Pick a project, then a session,\n"
    "and read every logged request chat-style.\n\n"
    "--port N binds the viewer to port N (default 8765); if busy, the next free\n"
    "port is used. The server binds to 127.0.0.1 only and is read-only."
)


def _parse_port(args: list[str]) -> int | None:
    """Parse ``[--port N]``; returns None if help/an error was printed."""
    port = _DEFAULT_PORT
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(_USAGE_TEXT, file=sys.stderr)
            return None
        if a == "--port":
            if i + 1 >= len(args):
                print("usage: --port expects a value", file=sys.stderr)
                return None
            try:
                port = int(args[i + 1])
            except ValueError:
                print(f"usage: --port expects an integer, got '{args[i + 1]}'", file=sys.stderr)
                return None
            if not (_MIN_PORT <= port <= _MAX_PORT):
                print(
                    f"usage: --port must be between {_MIN_PORT} and {_MAX_PORT}",
                    file=sys.stderr,
                )
                return None
            i += 2
            continue
        print(f"usage: unknown argument '{a}'", file=sys.stderr)
        return None
    return port


def run(args: list[str], tool_dir: Path) -> int:
    """Execute the `logs` subcommand."""
    port = _parse_port(args)
    if port is None:
        return 0 if (args and args[0] in ("-h", "--help")) else 1
    return serve(tool_dir, port)
