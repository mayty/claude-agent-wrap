# This file has been edited with the assistance of an AI tool.
"""HTTP server and static asset serving for the logs web viewer."""

import gzip
import json
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, override
from urllib.parse import parse_qs, urlparse

from agent_wrap.constants import LOGS_CONTENT_TYPES, PORT_SCAN_LIMIT
from agent_wrap.domain.logs.constants import (
    GZIP_COMPRESS_LEVEL,
    GZIP_MIN_BYTES,
    LOGS_PAGE_DIR,
    NDJSON_CONTENT_TYPE,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_wrap.domain.logs.cache import LogsCache
    from agent_wrap.domain.logs.stream import SessionStream


def get_handler(stream: SessionStream, cache: LogsCache) -> type[BaseHTTPRequestHandler]:  # noqa: C901
    class _Handler(SimpleHTTPRequestHandler):
        """Single-threaded HTTP handler for the logs viewer."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # Exact control over the four types the page is made of, rather than whatever
            # `mimetypes` has been taught locally: the base class consults this map before
            # falling back to the system tables, and `.js` in particular comes back from
            # some of them without a charset. Assigned here rather than as a class
            # attribute because typeshed declares it an instance variable, and it has to
            # land before `super().__init__`, which serves the request rather than
            # returning from it.
            self.extensions_map = dict(LOGS_CONTENT_TYPES)
            super().__init__(*args, directory=str(LOGS_PAGE_DIR), **kwargs)

        # Silence per-request log lines to stderr
        @override
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _resolve_project(self, qs: dict[str, list[str]]) -> tuple[list[str] | None, int | None]:
            """
            Resolve a ``project`` query param to ``(project hashes, project id)``, or send a 400.

            The hashes are the project's identity in the index -- one per member of a
            grouped transient project -- which is what a content read is scoped by. An
            empty list is a valid answer: a project whose log dir is not under the
            shared tree owns no hash, and has nothing indexed rather than being unknown.
            """
            raw = qs.get("project", [None])[0]
            if raw is None:
                self._send_json({"error": "missing project param"}, 400)
                return None, None
            try:
                project_id = int(raw)
            except ValueError, TypeError:
                self._send_json({"error": f"invalid project id: {raw!r}"}, 400)
                return None, None
            hashes = cache.get_project_hashes(project_id)
            if hashes is None:
                self._send_json({"error": f"unknown project id: {project_id}"}, 400)
                return None, None
            return hashes, project_id

        def _bounds(self, qs: dict[str, list[str]]) -> tuple[int, int | None] | None:
            """
            Parse ``from`` and ``limit`` into ``(from_index, limit)``, or send a 400.

            ``from`` defaults to 0 and ``limit`` to no cap. Both are refused when
            negative rather than clamped: a negative slice bound would silently return
            a tail from the *end* of the session, which is not what any caller means.
            """
            bounds: dict[str, int | None] = {}
            for name in ("from", "limit"):
                raw = qs.get(name, [None])[0]
                if raw is None:
                    bounds[name] = None
                    continue
                try:
                    value = int(raw)
                except ValueError, TypeError:
                    self._send_json({"error": f"invalid {name} value: {raw!r}"}, 400)
                    return None
                if value < 0:
                    self._send_json({"error": f"invalid {name} value: {raw!r}"}, 400)
                    return None
                bounds[name] = value
            return bounds["from"] or 0, bounds["limit"]

        @override
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path

            api = self._API_DISPATCH.get(path)
            if api is not None:
                api(self, parse_qs(parsed.query))
                return

            # The allow-list the base class does not have: it would otherwise serve any
            # file in the directory, and render a listing for the directory itself.
            # `translate_path` handles the traversal and containment questions.
            suffix = PurePosixPath("/index.html" if path == "/" else path).suffix
            if suffix not in self.extensions_map:
                self.send_error(404)
                return
            super().do_GET()

        # Cache-served list endpoints — no I/O of any kind on the request path
        #
        # Every one of these is answered from the cache's snapshot of the request
        # index, which the watcher's consumer thread refreshes. The `*-stat`
        # endpoints return a {rev, count} fingerprint the browser polls: `rev` is
        # the newest ingest instant in scope and `count` the number of indexed
        # sessions, which together move for every change either list can show.

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
            self._send_json(cache.get_sessions_fingerprint(project_id) or {"rev": None, "count": 0})

        def _handle_session_stat(self, qs: dict[str, list[str]]) -> None:
            _logs_dirs, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json({"error": "missing session param"}, 400)
                return
            self._send_json(
                cache.get_session_fingerprint(project_id, session_id) or {"rev": None, "count": 0}
            )

        # The one endpoint that reads content — straight off the request index
        #
        # No cache in front of it, and nothing to invalidate. The read is bounded by
        # the session the user opened rather than by the log tree, and its incremental
        # form -- `from`, which the browser's one-second tick uses -- returns the
        # records added since plus only the content those records introduced.

        def _handle_session(self, qs: dict[str, list[str]]) -> None:
            hashes, project_id = self._resolve_project(qs)
            if project_id is None:
                return
            session_id = qs.get("session", [None])[0]
            if not session_id:
                self._send_json({"error": "missing session param"}, 400)
                return

            bounds = self._bounds(qs)
            if bounds is None:
                return
            from_index, limit = bounds

            assert hashes is not None  # _resolve_project already returned success
            lines = stream.lines(
                hashes,
                session_id,
                cache.get_session_meta(project_id, session_id),
                from_index=from_index,
                limit=limit,
            )
            # Joined rather than written line by line: the response carries a
            # Content-Length and is gzipped, both of which need the whole body. What the
            # stream's batching bounds is the *read* -- the content held in memory while
            # assembling it -- and a session's payload is now megabytes where it used to
            # be tens of them.
            body = "".join(f"{line}\n" for line in lines)
            self._send_body(body.encode("utf-8"), NDJSON_CONTENT_TYPE)

        def _accepts_gzip(self) -> bool:
            header = self.headers.get("Accept-Encoding", "")
            # Split off q-values so "gzip;q=0.8, deflate" matches on the token alone.
            return any(
                part.split(";", 1)[0].strip().lower() == "gzip" for part in header.split(",")
            )

        def _send_body(self, body: bytes, content_type: str, status: int = 200) -> None:
            """
            Write *body*, compressing it when the client accepts gzip.

            The session and strings endpoints carry essentially all of the viewer's
            bytes -- one large session is tens of megabytes of highly repetitive JSON,
            and the 1 Hz tick re-sends it -- so this is the cheapest win available.
            Browsers decode ``Content-Encoding: gzip`` transparently, and ``fetch``
            hands the decoded stream to ``readNDJSONStream`` unchanged, so no client
            code changes.
            """
            if len(body) >= GZIP_MIN_BYTES and self._accepts_gzip():
                body = gzip.compress(body, GZIP_COMPRESS_LEVEL)
                content_encoding = "gzip"
            else:
                content_encoding = ""
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
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

        # Declared after the methods it names, so these are the function objects rather
        # than strings resolved by getattr at request time — a typo is a type error now.
        _API_DISPATCH: ClassVar[dict[str, Callable[..., None]]] = {
            "/api/projects": _handle_projects,
            "/api/sessions": _handle_sessions,
            "/api/session": _handle_session,
            "/api/session-stat": _handle_session_stat,
            "/api/sessions-stat": _handle_sessions_stat,
            "/api/projects-stat": _handle_projects_stat,
        }

    return _Handler


def bind_port(
    port: int,
    handler: type[BaseHTTPRequestHandler],
) -> ThreadingHTTPServer:
    for _offset in range(PORT_SCAN_LIMIT):
        try:
            return ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            port += 1
    msg = f"could not bind to any port in range [original, original+{PORT_SCAN_LIMIT})"
    raise RuntimeError(msg)
