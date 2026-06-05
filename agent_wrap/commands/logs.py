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
  from the callback's circular-reference handling; those are stripped here.
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


def resolve(obj: Any, strings: dict[str, str]) -> Any:
    """
    Recursively replace ``hash:<sha256>`` strings with their originals and drop
    the callback's circular-reference bookkeeping (``wrap-ref``/``wrap-ref-id``).

    Unknown hashes are left intact so a missing ``strings.jsonl`` entry is
    visible rather than silently blanked.
    """
    if isinstance(obj, str):
        return strings.get(obj, obj)
    if isinstance(obj, dict):
        return {k: resolve(v, strings) for k, v in obj.items() if k != "wrap-ref-id"}
    if isinstance(obj, list):
        return [
            resolve(v, strings)
            for v in obj
            if not (isinstance(v, str) and v.startswith("wrap-ref-id:"))
        ]
    return obj


# ---------------------------------------------------------------------------
# Record normalization
# ---------------------------------------------------------------------------


def normalize_record(rec: dict, strings: dict[str, str]) -> dict[str, Any]:
    """
    Reduce one raw log record to the shape the UI consumes.

    Pure (no I/O) so it can be unit-tested directly. Pulls the real prompt from
    ``request.proxy_server_request.body.data`` and the reply from
    ``response.choices[0].message``, resolving hashes throughout.
    """
    request = rec.get("request") or {}
    psr = request.get("proxy_server_request")
    data: dict[str, Any] = {}
    agent_id: str | None = None
    if isinstance(psr, dict):
        body = psr.get("body")
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            data = body["data"]
        # Subagent turns carry an ``x-claude-code-agent-id`` request header; the
        # main loop's requests have none. The id is short and unhashed, so it is
        # read straight from the headers dict. It groups a subagent's turns in
        # the viewer, which otherwise interleaves them with the main thread.
        headers = psr.get("headers")
        if isinstance(headers, dict):
            hdr_id = headers.get("x-claude-code-agent-id")
            if isinstance(hdr_id, str) and hdr_id:
                agent_id = hdr_id

    response = rec.get("response")
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

    out = {
        "ts": rec.get("ts"),
        "status": rec.get("status"),
        "model": rec.get("model"),
        "agent_id": agent_id,
        "messages": data.get("messages") or [],
        "system": data.get("system"),
        "tools": data.get("tools") or [],
        "response": reply,
        "usage": usage,
        "error": rec.get("error"),
    }
    return resolve(out, strings)


_ALIAS_NAME_RE = re.compile(r'"name"\s*:\s*"([^"]+)"')


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


def list_sessions(project: Path) -> list[dict[str, Any]]:
    """List sessions (newest first) across every provider in a project."""
    logs_dir = _logs_dir(project)
    if not logs_dir.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for provider_dir in logs_dir.iterdir():
        if not provider_dir.is_dir():
            continue
        for session_dir in provider_dir.iterdir():
            if not session_dir.is_dir():
                continue
            meta = _scan_session_meta(session_dir, provider_dir.name)
            if meta is not None:
                out.append(meta)
    out.sort(key=lambda s: s["last_ts"] or "", reverse=True)
    return out


def read_session(project: Path, provider: str, session_id: str) -> list[dict[str, Any]]:
    """Read and normalize every request in one session, in file order."""
    session_dir = _logs_dir(project) / provider / session_id
    messages_file = session_dir / "messages.jsonl"
    if not messages_file.is_file():
        return []

    strings = load_strings(session_dir)
    out: list[dict[str, Any]] = []
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
                out.append(normalize_record(rec, strings))
    except OSError:
        return []
    return out


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
                return
            self._send_json(list_sessions(project))
            return

        if path == "/api/session":
            project = self._resolve_project(params)
            provider = (params.get("provider") or [""])[0]
            session = (params.get("session") or [""])[0]
            if project is None or not provider or not session:
                self._send_json({"error": "missing project/provider/session"}, status=400)
                return
            self._send_json(read_session(project, provider, session))
            return

        self._serve_static(path)

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
