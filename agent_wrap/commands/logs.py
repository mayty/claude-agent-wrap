# This file has been edited with the assistance of an AI tool.
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

* ``messages.jsonl`` — one record per call. ``request`` is the proxy server
  request; the *real* Anthropic request lives at ``request.body.data``
  (``messages``/``system``/``tools``). The reply is OpenAI-shaped at
  ``response.choices[0].message`` with ``response.usage`` token counts.
  Timing lives in a ``timing`` object — ``{"start", "completionStart", "end"}``,
  each a Unix epoch-seconds float (or null) sourced from LiteLLM.
* ``strings.jsonl`` — ``{"hash": "hash:<sha256>", "original": ...}`` lines.
  Long strings in the record are replaced by ``hash:<sha256>`` pointers; we load
  this map and resolve them for display.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from agent_wrap.commands.stats import PriceSource, extract_usage, request_cache_ttl
from agent_wrap.lib.atomic import atomic_write_json
from agent_wrap.lib.grouping import orphaned_log_dirs, resolve_group
from agent_wrap.lib.usage_args import load_projects

if TYPE_CHECKING:
    from agent_wrap.providers.litellm_common.callback import LogRecord, MetaData

USAGE = "[--port N] [--stop]"
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

# Global, gitignored runtime state for the background viewer. The viewer is a
# host-level singleton (it serves every registered project), so a single state
# file under .agent-launches/ — alongside projects.txt — is the right home.
_STATE_FILE_NAME = "logs-server.json"
_STATE_DIR_NAME = ".agent-launches"
_LOG_FILE_NAME = "logs-server.log"

# Set by _spawn_background on the detached child so it resolves the same
# tool_dir (and thus the same state file) as the parent that launched it.
_TOOL_DIR_ENV = "AGENT_LOGS_TOOL_DIR"

# Parent → child handshake / stop-wait timing.
_SPAWN_TIMEOUT_SEC = 5.0
_STOP_TIMEOUT_SEC = 3.0
_POLL_INTERVAL_SEC = 0.05

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


def _resolve_hashes(obj: Any, strings: dict[str, str]) -> Any:
    """
    Replace ``hash:<sha256>`` strings with their originals.

    A lightweight tree walk. Unknown hashes are left intact so a missing
    ``strings.jsonl`` entry is visible rather than silently blanked.
    """
    if isinstance(obj, str):
        return strings.get(obj, obj)
    if isinstance(obj, dict):
        return {k: _resolve_hashes(v, strings) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_hashes(v, strings) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# Record normalization
# ---------------------------------------------------------------------------


def _extract_record_fields(
    rec: Any,
) -> tuple[dict[str, Any], str | None, dict[str, Any], dict[str, Any]]:
    """Extract (data, agent_id, reply, usage) from a raw or resolved record."""
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
    if isinstance(response, dict):
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict) and isinstance(first.get("message"), dict):
                reply = first["message"]
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    return data, agent_id, reply, usage


def normalize_record(rec: LogRecord, strings: dict[str, str]) -> dict[str, Any]:
    """
    Reduce one raw log record to the shape the UI consumes.

    Pure (no I/O) so it can be unit-tested directly. Pulls the real prompt
    from ``request.body.data`` and the reply from
    ``response.choices[0].message``, resolving ``hash:<sha256>`` pointers.
    """
    resolved = _resolve_hashes(rec, strings)

    data, agent_id, reply, usage = _extract_record_fields(resolved)

    return {
        "timing": resolved.get("timing"),
        "status": resolved.get("status"),
        "model": resolved.get("model"),
        "agent_id": agent_id,
        "messages": data.get("messages") or [],
        "system": data.get("system"),
        "tools": data.get("tools") or [],
        "response": reply,
        "usage": usage,
        "error": resolved.get("error"),
    }


def _enrich_with_costs(
    normalized: dict, raw_response: dict | None, provider: str, raw_request: dict | None = None
) -> dict[str, Any]:
    """
    Compute cost, cache pct, and token counts for one normalized record.

    Returns a dict of extra fields to merge into the record. Pure aside from
    the ``_PRICES`` lookup, which is in-memory after the first fetch per provider.
    """
    model = normalized.get("model") or ""
    usage = normalized.get("usage") or {}

    # The request's cache_control TTL attributes cache writes to a 5m/1h tier
    # when the response omits the split (the Bedrock case).
    request_ttl = request_cache_ttl(raw_request)

    # Use the canonical token extraction so field-resolution logic lives in one
    # place (extract_usage handles prompt_tokens/input_tokens fallback, etc.).
    norm_usage = extract_usage(raw_response, request_ttl)
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
        cost = _PRICES.compute_cost(provider, model, raw_response, request_ttl)

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


def extract_alias(rec: LogRecord) -> str | None:
    """
    Return Claude Code's kebab-case session name if ``rec`` is its naming call.

    Mirrors the callback's ``extract_session_alias`` so existing logs (written
    before the callback learned to persist metadata) still surface a
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


def extract_title(rec: LogRecord) -> str | None:
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


def _read_last_record_ts(messages_file: Path) -> float | None:
    r"""
    Read the ``timing.end`` epoch from the last JSON record in *messages_file*.

    Seeks to the last 1 MB, skips to the first ``\n`` to avoid landing in
    the middle of a multi-byte character, then walks lines backwards to
    find the last valid JSON record.
    """
    if not messages_file.is_file():
        return None

    try:
        size = messages_file.stat().st_size
        if size == 0:
            return None
        with messages_file.open("rb") as f:
            chunk_size = min(size, 1_048_576)
            f.seek(-chunk_size, os.SEEK_END)
            tail = f.read(chunk_size)
    except OSError:
        return None

    # If we started mid-file (not at offset 0), skip past the first newline
    # to avoid a partial line that could be cut mid-character.
    if chunk_size < size:
        nl = tail.find(b"\n")
        if nl != -1:
            tail = tail[nl + 1 :]

    tail_str = tail.decode("utf-8", errors="replace")
    for raw_line in reversed(tail_str.splitlines()):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        end = (rec.get("timing") or {}).get("end")
        if isinstance(end, (int, float)):
            return end

    return None


def _lightweight_logs_summary(logs_dir: Path) -> tuple[int, float | None]:
    """
    Return ``(session_count, max_last_ts)`` for a logs dir using minimal I/O.

    Counts session directories (deduplicating across providers) and reads
    the last record's timestamp only from the single ``messages.jsonl``
    with the highest modification time — the file most recently appended to,
    which is where the latest timestamp lives.
    """
    if not logs_dir.is_dir():
        return 0, None

    seen_sessions: set[str] = set()
    newest_file: Path | None = None
    newest_mtime: int = 0

    # rglob walks logs_dir/<provider>/<session_id>/messages.jsonl in one pass.
    for messages_file in logs_dir.rglob("messages.jsonl"):
        if not messages_file.is_file():
            continue

        # Deduplicate session_id across providers.
        session_id = messages_file.parent.name
        seen_sessions.add(session_id)

        # Track the file with the highest modification time.
        try:
            mtime = messages_file.stat().st_mtime_ns
        except OSError:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest_file = messages_file

    # Read only the single most-recently-written file — its last record
    # carries the latest timestamp across all sessions.
    max_last_ts: float | None = None
    if newest_file is not None:
        max_last_ts = _read_last_record_ts(newest_file)

    return len(seen_sessions), max_last_ts


def _lightweight_project_summary(project: Path) -> tuple[int, float | None]:
    """Lightweight summary for a project, via its ``.claude/litellm-logs`` dir."""
    return _lightweight_logs_summary(_logs_dir(project))


def _logs_dir(project: Path) -> Path:
    return project / ".claude" / "litellm-logs"


def _as_logs_dirs(project: Path | list[Path]) -> list[Path]:
    """
    Normalize a reader argument to a list of LiteLLM logs dirs to scan.

    Accepts either a single project :class:`~pathlib.Path` (the historical,
    per-project API still used by tests) — mapped to its ``.claude/litellm-logs``
    — or a list of logs dirs already resolved by the HTTP handler (a grouped
    transient project's members, or the synthetic ``<orphaned>`` group's central
    ``<hash>`` dirs, which *are* logs dirs and have no ``.claude`` wrapper).
    """
    return project if isinstance(project, list) else [_logs_dir(project)]


def list_groups(tool_dir: Path) -> list[dict[str, Any]]:
    """
    Group registered projects into transient projects by ``.agent_stats_leaf``.

    Returns one dict per group, ordered deterministically by group-root path so
    that a group's index is a stable id across requests. Each entry carries:

    * ``root`` — the group root :class:`~pathlib.Path` (marker dir, or the
      project itself when unmarked),
    * ``name`` — the custom marker name or the root's directory name,
    * ``paths`` — every member project :class:`~pathlib.Path` in the group,
    * ``logs_dirs`` — the LiteLLM logs dirs to scan for the group.

    Projects without a ``.claude/litellm-logs`` directory are skipped, mirroring
    the pre-grouping behaviour. Members are kept in registry order. A synthetic
    ``<orphaned>`` group is appended last (when present) for central log dirs left
    behind by deleted projects / stale registry entries — its ``logs_dirs`` are the
    central ``<hash>`` dirs themselves and it has no member ``paths``.
    """
    registry = tool_dir / ".agent-launches" / "projects.txt"
    if not registry.is_file():
        return []

    projects = load_projects(registry)
    names: dict[Path, str] = {}
    members: dict[Path, list[Path]] = {}
    for path in projects:
        if not _logs_dir(path).is_dir():
            continue
        root, name, _transient = resolve_group(path)
        if root not in members:
            members[root] = []
            names[root] = name
        members[root].append(path)

    # Sort by group-root path so ids are stable; callers re-sort the *public*
    # list (by recency) without disturbing this id assignment.
    groups: list[dict[str, Any]] = [
        {
            "root": root,
            "name": names[root],
            "paths": members[root],
            "logs_dirs": [_logs_dir(p) for p in members[root]],
        }
        for root in sorted(members)
    ]

    orphaned = orphaned_log_dirs(tool_dir, projects)
    if orphaned:
        groups.append(
            {
                "root": Path("<orphaned>"),
                "name": "<orphaned>",
                "paths": [],
                "logs_dirs": orphaned,
            }
        )
    return groups


def list_projects(tool_dir: Path) -> list[dict[str, Any]]:
    """List transient projects (grouped) that have LiteLLM logs."""
    out: list[dict[str, Any]] = []
    for idx, group in enumerate(list_groups(tool_dir)):
        session_count = 0
        max_last_ts: float | None = None
        for logs_dir in group["logs_dirs"]:
            count, last_ts = _lightweight_logs_summary(logs_dir)
            session_count += count
            if last_ts is not None and (max_last_ts is None or last_ts > max_last_ts):
                max_last_ts = last_ts
        if session_count == 0:
            continue
        out.append(
            {
                "id": idx,
                "path": str(group["root"]),
                "name": group["name"],
                "sessions": session_count,
                "last_ts": max_last_ts,
            }
        )
    out.sort(key=lambda p: p["last_ts"] or 0, reverse=True)
    return out


def _group_by_id(tool_dir: Path, group_id: int) -> list[Path] | None:
    """
    Return the logs dirs to scan for a group index, or None if unknown.

    The session readers take logs dirs directly (see :func:`_as_logs_dirs`), so
    this returns the group's ``logs_dirs`` — a project's ``.claude/litellm-logs``
    for normal groups, or the central ``<hash>`` dirs for the ``<orphaned>`` group.
    """
    groups = list_groups(tool_dir)
    if 0 <= group_id < len(groups):
        return groups[group_id]["logs_dirs"]
    return None


class _SessionMeta:
    """Accumulates cheap per-session metadata as records are scanned."""

    def __init__(self) -> None:
        self.count = 0
        self.first_ts: float | None = None
        self.last_ts: float | None = None
        self.models: set[str] = set()
        self.derived_alias: str | None = None
        self.derived_title: str | None = None

    def add(self, rec: LogRecord) -> None:
        self.count += 1
        timing = rec.get("timing") or {}
        start = timing.get("start")
        if isinstance(start, (int, float)) and self.first_ts is None:
            self.first_ts = start
        end = timing.get("end")
        if isinstance(end, (int, float)):
            self.last_ts = end
        model = rec.get("model")
        if isinstance(model, str):
            self.models.add(model.rsplit("/", 1)[-1])
        alias = extract_alias(rec)
        if alias:
            self.derived_alias = alias
        title = extract_title(rec)
        if title:
            self.derived_title = title


def _read_meta_json(session_dir: Path) -> MetaData | None:
    """
    Read ``meta.json`` if it exists and is not older than ``messages.jsonl``.

    Returns the parsed dict on success, or ``None`` when the cache is missing,
    stale, or corrupt so the caller can fall back to a full scan.
    """
    meta_file = session_dir / "meta.json"
    messages_file = session_dir / "messages.jsonl"
    if not meta_file.is_file() or not messages_file.is_file():
        return None
    try:
        meta_mtime = meta_file.stat().st_mtime_ns
        msg_mtime = messages_file.stat().st_mtime_ns
    except OSError:
        return None
    # meta.json is written *after* messages.jsonl by the callback; if it is
    # older, a write was interrupted and the cache may be incomplete.
    if meta_mtime < msg_mtime:
        return None
    try:
        cached = json.loads(meta_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    # Reject a pre-timing-format cache: `last_ts` used to be an ISO string but
    # is now an epoch float. A leftover string entry (e.g. an old sidecar that
    # rewrote meta.json) would crash the float-keyed session sort, so treat it
    # as stale and force a rescan that regenerates numeric timestamps.
    last_ts = cached.get("last_ts")
    if last_ts is not None and not isinstance(last_ts, (int, float)):
        return None
    return cached


def _write_meta_json(session_dir: Path, meta: MetaData) -> None:
    """Write ``meta.json`` atomically.  Best-effort; never raises."""
    with contextlib.suppress(OSError):
        atomic_write_json(session_dir / "meta.json", meta)


def _scan_session_meta(session_dir: Path, provider: str) -> dict[str, Any] | None:
    """
    Cheap metadata for one session: count, first/last ts, models, alias.

    Checks a ``meta.json`` cache first (maintained by the LiteLLM callback);
    falls back to a full scan of ``messages.jsonl`` when the cache is missing
    or stale, and seeds the cache after a fallback scan.
    """
    # Fast path: use the callback-maintained cache when available.
    cached = _read_meta_json(session_dir)
    if cached is not None:
        return {
            "provider": provider,
            "session_id": session_dir.name,
            "alias": cached.get("alias"),
            "title": cached.get("title"),
            "count": cached.get("count", 0),
            "first_ts": cached.get("first_ts"),
            "last_ts": cached.get("last_ts"),
            "models": cached.get("models") or [],
        }

    # Slow path: full scan (existing behavior).
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

    # Seed the cache so subsequent reads hit the fast path. last_ts may be None
    # when no record carried a timing.end (e.g. an all-failure session); the
    # cache, the float sort, and _merge_session_meta all tolerate that.
    _write_meta_json(
        session_dir,
        {
            "count": meta.count,
            "last_ts": meta.last_ts,
            "models": sorted(meta.models),
            "alias": meta.derived_alias,
            "title": meta.derived_title,
        },
    )

    return {
        "provider": provider,
        "session_id": session_dir.name,
        "alias": meta.derived_alias,
        "title": meta.derived_title,
        "count": meta.count,
        "first_ts": meta.first_ts,
        "last_ts": meta.last_ts,
        "models": sorted(meta.models),
    }


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


def list_sessions(project: Path | list[Path]) -> list[dict[str, Any]]:
    """
    List sessions (newest first) across every provider in a project.

    Sessions with the same ``session_id`` across different providers — or across
    the member projects of a grouped transient project — are merged into a single
    entry so a mid-session provider switch (or grouping) doesn't produce duplicate
    rows in the viewer.
    """
    by_session: dict[str, dict[str, Any]] = {}
    for logs_dir in _as_logs_dirs(project):
        if not logs_dir.is_dir():
            continue
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
    out.sort(key=lambda s: s["last_ts"] or 0, reverse=True)
    return out


def session_fingerprint(project: Path | list[Path], session_id: str) -> dict[str, Any]:
    """
    Return a combined change-marker for a session across all providers.

    Returns ``{"mtime": max_mtime_ns, "size": sum_sizes}`` across every provider
    directory (and every member project of a group) that holds this session, so a
    new record from any provider triggers a refresh in the polling loop.  Returns
    ``{"mtime": None, "size": None}`` when no provider has the session.
    """
    best_mtime: int | None = None
    total_size: int | None = None
    found = False

    for logs_dir in _as_logs_dirs(project):
        if not logs_dir.is_dir():
            continue
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


def sessions_fingerprint(project: Path | list[Path]) -> dict[str, Any]:
    """
    Return a change-marker for all sessions in a project.

    Like :func:`session_fingerprint` but across every session directory (and every
    member project of a group) so the frontend's sessions-list poll can detect new
    sessions, new records, and metadata changes without re-reading every
    messages.jsonl.

    Returns ``{"mtime": max_mtime_ns, "size": sum_sizes}`` across every
    ``messages.jsonl`` under the project's logs directory.  Returns
    ``{"mtime": None, "size": None}`` when no sessions exist.
    """
    best_mtime: int | None = None
    total_size: int | None = None
    found = False

    for logs_dir in _as_logs_dirs(project):
        if not logs_dir.is_dir():
            continue
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

    # Read every raw record first so we capture a consistent snapshot of
    # messages.jsonl.  Strings are loaded *afterwards* because the callback
    # writes hashes to strings.jsonl before appending the record — loading
    # strings after records guarantees every hash we encounter is resolved.
    meta = _SessionMeta()
    raw_records: list[dict[str, Any]] = []
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
                raw_records.append(rec)
                meta.add(rec)
    except OSError:
        pass

    if meta.count == 0:
        return [], None

    strings = load_strings(session_dir)
    records: list[dict[str, Any]] = []
    for rec in raw_records:
        raw_response = rec.get("response")
        normalized = normalize_record(rec, strings)  # type: ignore[arg-type]
        enriched = _enrich_with_costs(normalized, raw_response, provider, rec.get("request"))
        normalized.update(enriched)
        records.append(normalized)

    entry: dict[str, Any] = {
        "provider": provider,
        "session_id": session_id,
        "alias": meta.derived_alias,
        "title": meta.derived_title,
        "count": meta.count,
        "first_ts": meta.first_ts,
        "last_ts": meta.last_ts,
        "models": sorted(meta.models),
    }
    return records, entry


def read_session(project: Path | list[Path], session_id: str) -> dict[str, Any]:
    """
    Read and normalize every request in one session across all providers.

    When a session spans multiple providers (e.g. the user switched mid-session) —
    or multiple member projects of a grouped transient project — records from every
    provider directory are loaded and merge-sorted by ``ts`` so the chat view shows
    a single chronological thread.

    Returns ``{"reqs": [...], "session_meta": {...}}`` where *session_meta* has
    the same shape as one entry from :func:`list_sessions` (or ``None`` when no
    records exist).
    """
    all_records: list[dict[str, Any]] = []
    combined_meta: dict[str, Any] | None = None

    for logs_dir in _as_logs_dirs(project):
        if not logs_dir.is_dir():
            continue
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

    all_records.sort(key=lambda r: (r.get("timing") or {}).get("start") or 0)
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

    # Both bound as class attributes in _serve_foreground().
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

    def _resolve_session(self, params: dict[str, list[str]]) -> tuple[list[Path], str] | None:
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

    def _resolve_project(self, params: dict[str, list[str]]) -> list[Path] | None:
        """Resolve the ``project`` query param (a group id) to its member paths."""
        raw = (params.get("project") or [""])[0]
        try:
            group_id = int(raw)
        except ValueError:
            return None
        return _group_by_id(self.tool_dir, group_id)


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


# ---------------------------------------------------------------------------
# Background-server state (PID/port handshake)
# ---------------------------------------------------------------------------
#
# The viewer now runs detached: `agent logs` re-execs itself with the hidden
# `--foreground` flag via subprocess (mirroring ops/statusline.py's detached
# Popen idiom), and the two processes coordinate through a small JSON state
# file under .agent-launches/. The parent prints the connect line; the child
# is silent (its stdout/stderr go to a logfile).


def _state_dir(tool_dir: Path) -> Path:
    return tool_dir / _STATE_DIR_NAME


def _state_file(tool_dir: Path) -> Path:
    return _state_dir(tool_dir) / _STATE_FILE_NAME


def _read_state(tool_dir: Path) -> dict[str, Any] | None:
    """Read the viewer state file, or None when missing/corrupt."""
    try:
        raw = _state_file(tool_dir).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and isinstance(data.get("pid"), int)
        and isinstance(data.get("port"), int)
    ):
        return data
    return None


def _write_state(tool_dir: Path, pid: int, port: int) -> None:
    """Write the viewer state file atomically (tmp + replace)."""
    atomic_write_json(_state_file(tool_dir), {"pid": pid, "port": port})


def _pid_alive(pid: int) -> bool:
    """
    Return True if *pid* refers to a live process we can see.

    A zombie (terminated but not yet reaped — common when the launching parent
    has already exited and the orphan's reaper is slow) still answers
    ``os.kill(pid, 0)``, so it is explicitly treated as dead via /proc. The
    wrapper only runs on Linux, where /proc is always present.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by someone else — still "alive".
        return True
    except OSError:
        return False
    # Treat a zombie as dead: its server socket is already closed, but the PID
    # lingers in the table until reaped. Best-effort — if /proc is unreadable,
    # fall back to "alive" (the os.kill above already confirmed the PID exists).
    with contextlib.suppress(OSError, IndexError):
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # Fields: "pid (comm) state ..."; comm may contain spaces/parens, so
        # split after the final ')' to read the single-char state code.
        state = stat.rpartition(")")[2].split()[0]
        if state == "Z":
            return False
    return True


def _running_server(tool_dir: Path) -> dict[str, Any] | None:
    """
    Return the state dict if a live viewer is running, else None.

    A state file whose PID is dead is treated as stale and removed so the
    caller can spawn a fresh server.
    """
    state = _read_state(tool_dir)
    if state is None:
        return None
    if _pid_alive(state["pid"]):
        return state
    with contextlib.suppress(OSError):
        _state_file(tool_dir).unlink()
    return None


def _connect_line(port: int) -> str:
    return f"LiteLLM log viewer running at http://127.0.0.1:{port}"


def _serve_foreground(tool_dir: Path, port: int) -> int:
    """
    Run the viewer in the foreground until SIGTERM/SIGINT. Returns an exit code.

    This is the body of the detached child (`agent logs --foreground`). It binds
    the server, records {pid, port} in the state file, then blocks. It stays
    silent on stdout — the parent prints the user-facing connect line.

    serve_forever() runs on a daemon thread while the main thread blocks on a
    stop Event; the signal handler sets the Event and the main thread performs
    shutdown(). Calling shutdown() from a handler running *on* the serve_forever
    thread would deadlock, hence the split.
    """
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

    _write_state(tool_dir, os.getpid(), actual_port)

    stop = threading.Event()

    def _handle(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        with contextlib.suppress(OSError):
            _state_file(tool_dir).unlink()
    return 0


def _spawn_background(tool_dir: Path, port: int) -> int:
    """
    Spawn the detached viewer and print the connect line. Returns an exit code.

    Re-execs `python -m agent_wrap logs --foreground --port <port>` detached,
    then waits for the child to record {pid, port} in the state file before
    reporting the actual bound port.
    """
    state_dir = _state_dir(tool_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / _LOG_FILE_NAME

    try:
        logfile = log_path.open("ab")
    except OSError as exc:
        print(f"agent logs: cannot open log file {log_path}: {exc}", file=sys.stderr)
        return 1

    # Pin the child to the parent's tool_dir so both sides agree on where the
    # state file lives (the child would otherwise re-derive it from __main__).
    child_env = {**os.environ, _TOOL_DIR_ENV: str(tool_dir)}
    with logfile:
        proc = subprocess.Popen(
            [sys.executable, "-m", "agent_wrap", "logs", "--foreground", "--port", str(port)],
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=logfile,
            start_new_session=True,
            env=child_env,
        )

    # Wait for the child to publish its state, distinguishing success from an
    # early exit (missing assets, no free port) and from a timeout.
    deadline = time.monotonic() + _SPAWN_TIMEOUT_SEC
    while time.monotonic() < deadline:
        state = _read_state(tool_dir)
        if state is not None and state["pid"] == proc.pid:
            print(_connect_line(state["port"]))
            return 0
        if proc.poll() is not None:
            print(
                f"agent logs: viewer exited on startup (see {log_path})",
                file=sys.stderr,
            )
            return 1
        time.sleep(_POLL_INTERVAL_SEC)

    print(
        f"agent logs: viewer did not start within {_SPAWN_TIMEOUT_SEC:g}s (see {log_path})",
        file=sys.stderr,
    )
    with contextlib.suppress(OSError):
        proc.terminate()
    return 1


def _stop(tool_dir: Path) -> int:
    """Stop the background viewer. Returns an exit code (always 0)."""
    state = _running_server(tool_dir)
    if state is None:
        print("agent logs: no viewer is running.")
        return 0

    pid = state["pid"]
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, signal.SIGTERM)

    deadline = time.monotonic() + _STOP_TIMEOUT_SEC
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(_POLL_INTERVAL_SEC)

    with contextlib.suppress(OSError):
        _state_file(tool_dir).unlink()
    print("agent logs: viewer stopped.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_USAGE_TEXT = (
    "Usage: agent logs [--port N] [--stop]\n\n"
    "Starts a local web viewer for the LiteLLM request logs written under each\n"
    "project's .claude/litellm-logs/ directory. Pick a project, then a session,\n"
    "and read every logged request chat-style.\n\n"
    "The viewer runs in the background and prints its connect line; if one is\n"
    "already running, the existing connect line is reprinted (the port is\n"
    "ignored).\n\n"
    "--port N binds the viewer to port N (default 8765); if busy, the next free\n"
    "port is used. The server binds to 127.0.0.1 only and is read-only.\n"
    "--stop stops the background viewer."
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
    # A detached child is pinned to its launching parent's tool_dir so both
    # sides resolve the same state file (see _TOOL_DIR_ENV / _spawn_background).
    env_dir = os.environ.get(_TOOL_DIR_ENV)
    if env_dir:
        tool_dir = Path(env_dir)

    if "--stop" in args:
        if args != ["--stop"]:
            print("usage: agent logs --stop (takes no other arguments)", file=sys.stderr)
            return 1
        return _stop(tool_dir)

    # `--foreground` is a hidden internal flag: the re-exec'd child that actually
    # runs the blocking server. Strip it before port parsing so _parse_port (and
    # its tests) stay unchanged, and keep it out of USAGE/bashrc completion.
    foreground = "--foreground" in args
    if foreground:
        args = [a for a in args if a != "--foreground"]

    port = _parse_port(args)
    if port is None:
        return 0 if (args and args[0] in ("-h", "--help")) else 1

    if foreground:
        return _serve_foreground(tool_dir, port)

    running = _running_server(tool_dir)
    if running is not None:
        print(_connect_line(running["port"]))
        return 0

    return _spawn_background(tool_dir, port)
