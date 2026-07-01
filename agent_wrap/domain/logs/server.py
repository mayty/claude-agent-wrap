# This file has been edited with the assistance of an AI tool.
"""HTTP server and static asset serving for the logs web viewer."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_wrap.constants import LOGS_CONTENT_TYPES
from agent_wrap.domain.logs.constants import LOGS_PAGE_DIR, PORT_SCAN_LIMIT
from agent_wrap.domain.logs.io import (
    group_by_id,
    list_groups,
    list_projects,
    list_sessions,
    projects_fingerprint,
    read_session,
    session_fingerprint,
    sessions_fingerprint,
)


def resolve_static(path: str, *, root: Path | None = None) -> Path | None:
    """
    Map a URL path to a file inside the ``logs_page/`` directory.

    The *root* parameter is only exposed for tests; production callers rely on
    the module-level ``LOGS_PAGE_DIR`` default.
    """
    if root is None:
        root = LOGS_PAGE_DIR
    path = path.lstrip("/")
    # Root path serves index.html
    if path in ("", "/"):
        path = "index.html"
    url_path = Path(path)
    # Prevent directory traversal
    if ".." in url_path.parts or url_path.is_absolute():
        return None
    # Only serve known extensions
    if url_path.suffix not in LOGS_CONTENT_TYPES:
        return None
    candidate = (root / path).resolve()
    # Confirm the resolved path stays within the logs_page/ directory
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


class _Handler(BaseHTTPRequestHandler):
    """Single-threaded HTTP handler for the logs viewer."""

    # Set by bind_port before the server starts.  Must be set before any request
    # is served because read_session requires it.
    pricing: Any = None
    stats_service: Any = None

    # Silence per-request log lines to stderr
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    def _resolve_project(self, qs: dict[str, list[str]]) -> list[Path] | None:
        """Resolve a ``project`` query param to logs dirs, or send a 400 error."""
        raw = qs.get("project", [None])[0]
        if raw is None:
            self._send_json({"error": "missing project param"}, 400)
            return None
        try:
            project_id = int(raw)
        except (ValueError, TypeError):
            self._send_json({"error": f"invalid project id: {raw!r}"}, 400)
            return None
        logs_dirs = group_by_id(project_id, self.stats_service)
        if logs_dirs is None:
            self._send_json({"error": f"unknown project id: {project_id}"}, 400)
            return None
        return logs_dirs

    def do_GET(self):  # noqa: C901, PLR0911, PLR0912
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # API endpoints
        if path == "/api/groups":
            return self._send_json(list_groups(self.stats_service))  # type: ignore[call-arg]
        if path == "/api/projects":
            return self._send_json(list_projects(self.stats_service))
        if path == "/api/sessions":
            logs_dirs = self._resolve_project(qs)
            if logs_dirs is None:
                return None
            return self._send_json(list_sessions(logs_dirs))
        if path == "/api/session":
            logs_dirs = self._resolve_project(qs)
            if logs_dirs is None:
                return None
            session_id = qs.get("session", [None])[0]
            if not session_id:
                return self._send_json({"error": "missing session param"}, 400)
            return self._send_json(read_session(logs_dirs, session_id, pricing=self.pricing))
        if path == "/api/session-stat":
            logs_dirs = self._resolve_project(qs)
            if logs_dirs is None:
                return None
            session_id = qs.get("session", [None])[0]
            if not session_id:
                return self._send_json({"error": "missing session param"}, 400)
            return self._send_json(session_fingerprint(logs_dirs, session_id))
        if path == "/api/sessions-stat":
            logs_dirs = self._resolve_project(qs)
            if logs_dirs is None:
                return None
            return self._send_json(sessions_fingerprint(logs_dirs))
        if path == "/api/projects-stat":
            return self._send_json(projects_fingerprint(self.stats_service))

        # Fall through to static file serving
        return self._serve_static(path)

    def _send_json(self, data: Any, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        sf = resolve_static(path)
        if sf is None:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        content_type = LOGS_CONTENT_TYPES.get(sf.suffix, "application/octet-stream")
        body = sf.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def bind_port(port: int, pricing: Any = None, stats_service: Any = None) -> ThreadingHTTPServer:
    """Bind a ``ThreadingHTTPServer`` to the given port."""
    if pricing is not None:
        _Handler.pricing = pricing
    if stats_service is not None:
        _Handler.stats_service = stats_service
    for _offset in range(PORT_SCAN_LIMIT):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        except OSError:  # noqa: PERF203
            port += 1
    msg = f"could not bind to any port in range [original, original+{PORT_SCAN_LIMIT})"
    raise RuntimeError(msg)
