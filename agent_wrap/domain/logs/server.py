# This file has been edited with the assistance of an AI tool.
"""HTTP server and static asset serving for the logs web viewer."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, cast, override
from urllib.parse import parse_qs, urlparse

from agent_wrap.constants import LOGS_CONTENT_TYPES, PORT_SCAN_LIMIT
from agent_wrap.domain.logs.constants import LOGS_PAGE_DIR
from agent_wrap.domain.logs.io import (
    read_session,
    read_strings,
)

if TYPE_CHECKING:
    from agent_wrap.domain.logs.cache import LogsCache
    from agent_wrap.domain.pricing.service import PricingService


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


def get_handler(pricing: PricingService, cache: LogsCache) -> type[BaseHTTPRequestHandler]:  # noqa: C901
    class _Handler(BaseHTTPRequestHandler):
        """Single-threaded HTTP handler for the logs viewer."""

        # Silence per-request log lines to stderr
        @override
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _resolve_project(
            self, qs: dict[str, list[str]]
        ) -> tuple[list[Path] | None, int | None]:
            """Resolve a ``project`` query param to ``(logs_dirs, project_id)``, or send a 400 error."""
            raw = qs.get("project", [None])[0]
            if raw is None:
                self._send_json({"error": "missing project param"}, 400)
                return None, None
            try:
                project_id = int(raw)
            except ValueError, TypeError:
                self._send_json({"error": f"invalid project id: {raw!r}"}, 400)
                return None, None
            logs_dirs = cache.get_logs_dirs(project_id)
            if logs_dirs is None:
                self._send_json({"error": f"unknown project id: {project_id}"}, 400)
                return None, None
            return logs_dirs, project_id

        _API_DISPATCH: ClassVar[dict[str, str]] = {
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
            parsed = urlparse(self.path)
            path = parsed.path
            qs = parse_qs(parsed.query)

            method_name = self._API_DISPATCH.get(path)
            if method_name is not None:
                getattr(self, method_name)(qs)
            else:
                self._serve_static(path)

        # ------------------------------------------------------------------
        # Cache-served meta endpoints (no disk I/O on the request path)
        # ------------------------------------------------------------------

        def _handle_projects(self, _qs: dict[str, list[str]]) -> None:
            self._send_json(cache.get_projects())

        def _handle_projects_stat(self, _qs: dict[str, list[str]]) -> None:
            self._send_json(cache.get_projects_fingerprint())

        def _handle_sessions(self, qs: dict[str, list[str]]) -> None:
            _logs_dirs, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            self._send_json(cache.get_sessions(project_id) or [])

        def _handle_sessions_stat(self, qs: dict[str, list[str]]) -> None:
            _logs_dirs, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            self._send_json(
                cache.get_sessions_fingerprint(project_id) or {"mtime": None, "size": None}
            )

        def _handle_session_stat(self, qs: dict[str, list[str]]) -> None:
            _logs_dirs, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json({"error": "missing session param"}, 400)
                return
            self._send_json(
                cache.get_session_fingerprint(project_id, session_id)
                or {"mtime": None, "size": None}
            )

        # ------------------------------------------------------------------
        # Session / strings — hot cache with disk fallback
        # ------------------------------------------------------------------

        def _handle_session(self, qs: dict[str, list[str]]) -> None:
            logs_dirs, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json({"error": "missing session param"}, 400)
                return

            # Parse optional `from` query parameter.
            from_val = 0
            raw_from = qs.get("from", [None])[0]
            if raw_from is not None:
                try:
                    from_val = int(raw_from)
                except ValueError, TypeError:
                    self._send_json({"error": f"invalid from value: {raw_from!r}"}, 400)
                    return

            # --- Hot cache lookup (full and partial requests) ---
            hot = cache.get_hot_session(project_id, session_id)
            if hot is not None:
                records, _strings = hot
                if from_val > 0:
                    # Keep the meta line at index 0; slice request records from
                    # from_val onward (records[1] is req_0).
                    records = [records[0], *records[from_val + 1 :]]
                ndjson_body = "\n".join(json.dumps(r, default=str) for r in records) + "\n"
                body = ndjson_body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # --- Disk read (hot cache miss) ---
            assert logs_dirs is not None  # _resolve_project already returned success
            result = read_session(logs_dirs, session_id, pricing=pricing, from_index=from_val)
            strings = read_strings(logs_dirs, session_id)

            meta_line: dict[str, Any] = {"__type__": "session_meta"}
            if result["session_meta"] is not None:
                meta_line.update(result["session_meta"])
            lines: list[dict[str, Any]] = cast("list[dict[str, Any]]", [meta_line, *result["reqs"]])
            ndjson_body = "\n".join(json.dumps(r, default=str) for r in lines) + "\n"

            # Only cache full responses (the full list, not the NDJSON string).
            if from_val == 0:
                cache.set_hot_session(project_id, session_id, lines, strings)

            body = ndjson_body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _handle_strings(self, qs: dict[str, list[str]]) -> None:
            logs_dirs, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json({"error": "missing session param"}, 400)
                return
            assert cache is not None

            # Check hot cache first.
            hot = cache.get_hot_session(project_id, session_id)
            if hot is not None:
                _records, strings = hot
                body = strings.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            # Read from disk.
            assert logs_dirs is not None  # _resolve_project already returned success
            content = read_strings(logs_dirs, session_id)
            # Store alongside the hot session (even if the full session wasn't cached
            # yet, keep strings warm for the imminent session fetch).
            body = content.encode("utf-8")
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

    return _Handler


def bind_port(
    port: int,
    handler: type[BaseHTTPRequestHandler],
) -> ThreadingHTTPServer:
    """Bind a ``ThreadingHTTPServer`` to the given port."""
    for _offset in range(PORT_SCAN_LIMIT):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            port += 1
    msg = f"could not bind to any port in range [original, original+{PORT_SCAN_LIMIT})"
    raise RuntimeError(msg)
