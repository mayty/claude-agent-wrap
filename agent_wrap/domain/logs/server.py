# This file has been edited with the assistance of an AI tool.
"""HTTP server and static asset serving for the logs web viewer."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
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
    read_strings,
    session_fingerprint,
    sessions_fingerprint,
)

if TYPE_CHECKING:
    from agent_wrap.domain.pricing.service import PricingService
    from agent_wrap.domain.stats.service import StatsService


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
    pricing: PricingService | None = None
    stats_service: StatsService | None = None

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
        assert self.stats_service is not None  # set by bind_port before server starts
        logs_dirs = group_by_id(project_id, self.stats_service)
        if logs_dirs is None:
            self._send_json({"error": f"unknown project id: {project_id}"}, 400)
            return None
        return logs_dirs

    _API_DISPATCH: ClassVar[dict[str, str]] = {
        "/api/groups": "_handle_groups",
        "/api/projects": "_handle_projects",
        "/api/sessions": "_handle_sessions",
        "/api/session": "_handle_session",
        "/api/session-stat": "_handle_session_stat",
        "/api/sessions-stat": "_handle_sessions_stat",
        "/api/projects-stat": "_handle_projects_stat",
        "/api/strings": "_handle_strings",
    }

    def do_GET(self) -> None:
        # Set by bind_port before the server starts — guaranteed non-None at runtime.
        assert self.stats_service is not None
        assert self.pricing is not None

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        method_name = self._API_DISPATCH.get(path)
        if method_name is not None:
            getattr(self, method_name)(qs)
        else:
            self._serve_static(path)

    def _handle_groups(self, _qs: dict[str, list[str]]) -> None:
        assert self.stats_service is not None
        self._send_json(list_groups(self.stats_service))

    def _handle_projects(self, _qs: dict[str, list[str]]) -> None:
        assert self.stats_service is not None
        self._send_json(list_projects(self.stats_service))

    def _handle_sessions(self, qs: dict[str, list[str]]) -> None:
        logs_dirs = self._resolve_project(qs)
        if logs_dirs is None:
            return
        self._send_json(list_sessions(logs_dirs))

    def _handle_session(self, qs: dict[str, list[str]]) -> None:
        assert self.pricing is not None
        logs_dirs = self._resolve_project(qs)
        if logs_dirs is None:
            return
        session_id = qs.get("session", [None])[0]
        if not session_id:
            self._send_json({"error": "missing session param"}, 400)
            return
        self._send_json(read_session(logs_dirs, session_id, pricing=self.pricing))

    def _handle_session_stat(self, qs: dict[str, list[str]]) -> None:
        logs_dirs = self._resolve_project(qs)
        if logs_dirs is None:
            return
        session_id = qs.get("session", [None])[0]
        if not session_id:
            self._send_json({"error": "missing session param"}, 400)
            return
        self._send_json(session_fingerprint(logs_dirs, session_id))

    def _handle_sessions_stat(self, qs: dict[str, list[str]]) -> None:
        logs_dirs = self._resolve_project(qs)
        if logs_dirs is None:
            return
        self._send_json(sessions_fingerprint(logs_dirs))

    def _handle_projects_stat(self, _qs: dict[str, list[str]]) -> None:
        assert self.stats_service is not None
        self._send_json(projects_fingerprint(self.stats_service))

    def _handle_strings(self, qs: dict[str, list[str]]) -> None:
        logs_dirs = self._resolve_project(qs)
        if logs_dirs is None:
            return
        session_id = qs.get("session", [None])[0]
        if not session_id:
            self._send_json({"error": "missing session param"}, 400)
            return
        body = read_strings(logs_dirs, session_id).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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


def bind_port(
    port: int,
    pricing: PricingService | None = None,
    stats_service: StatsService | None = None,
) -> ThreadingHTTPServer:
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
