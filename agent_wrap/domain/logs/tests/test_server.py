from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.logs.server import bind_port, resolve_static
from agent_wrap.domain.pricing.service import PricingService
from agent_wrap.domain.stats.service import StatsService

if TYPE_CHECKING:
    from pathlib import Path

# --- resolve_static (path mapping + traversal safety) ---


def test_resolve_static_maps_root_to_index(tmp_path: Path):
    assert resolve_static("/", root=tmp_path) == (tmp_path / "index.html").resolve()


def test_resolve_static_maps_named_asset(tmp_path: Path):
    assert resolve_static("/app.js", root=tmp_path) == (tmp_path / "app.js").resolve()
    assert resolve_static("/styles.css", root=tmp_path) == (tmp_path / "styles.css").resolve()


def test_resolve_static_rejects_traversal(tmp_path: Path):
    page = tmp_path / "logs_page"
    page.mkdir()
    # Escaping the page dir must be refused, not resolved to a sibling file.
    assert resolve_static("/../logs.py", root=page) is None
    assert resolve_static("/../../etc/passwd", root=page) is None


# --- HTTP handler helpers ----------------------------------------------------


def _find_free_port() -> int:
    """Return a free port by asking the OS, then release it immediately."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _get_json(port: int, path: str) -> tuple[int, Any]:
    """GET *path* and return (status_code, parsed_json_body)."""
    req = urllib.request.Request(_url(port, path))  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def _start_server(port: int, stats_service: StatsService) -> threading.Thread:
    """Start the logs HTTP server on *port* in a daemon thread and return the thread."""
    pricing = Mock(spec=PricingService)
    pricing.request_cache_ttl.return_value = None
    pricing.extract_usage.return_value = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
    }
    pricing.compute_cost.return_value = 0.001
    server = bind_port(port, pricing=pricing, stats_service=stats_service)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Give the server a moment to start accepting connections.
    time.sleep(0.1)
    return t


# --- group / stats helpers ---------------------------------------------------


def _write_session(project: Path, provider: str, session_id: str) -> Path:
    """Write a minimal messages.jsonl for *session_id* and return its dir."""
    sdir = project / ".claude" / "litellm-logs" / provider / session_id
    sdir.mkdir(parents=True)
    rec: dict[str, Any] = {
        "timing": {"start": 1000000.0, "completionStart": None, "end": 1000001.0},
        "model": "m/test",
        "response": {},
    }
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return sdir


# --- API endpoint tests ------------------------------------------------------


class TestAPIEndpoints:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self.port = _find_free_port()
        self.project = tmp_path / "testproj"
        _write_session(self.project, "litellm-bedrock", "abc12345-6789-abcd-ef01-234567890abc")
        # list_groups needs the registry file to exist; its contents are
        # served by the mocked StatsService.load_projects.
        reg = tmp_path / ".agent-launches" / "projects.txt"
        reg.parent.mkdir(parents=True, exist_ok=True)
        reg.write_text("", encoding="utf-8")
        stats = Mock(spec=StatsService)
        stats.load_projects.return_value = [self.project]
        stats.resolve_group.return_value = (self.project, self.project.name, False)
        stats.orphaned_log_dirs.return_value = []  # type: ignore[implicit-any-empty-container]
        _start_server(self.port, stats)

    # --- /api/sessions -------------------------------------------------------

    def test_sessions_returns_list(self):
        status, body = _get_json(self.port, "/api/sessions?project=0")
        assert status == 200
        assert isinstance(body, list)
        assert len(body) == 1
        assert body[0]["session_id"] == "abc12345-6789-abcd-ef01-234567890abc"
        assert body[0]["count"] == 1

    def test_sessions_missing_project_returns_400(self):
        status, body = _get_json(self.port, "/api/sessions")
        assert status == 400
        assert "missing project param" in body["error"]

    def test_sessions_invalid_project_returns_400(self):
        status, body = _get_json(self.port, "/api/sessions?project=notanumber")
        assert status == 400
        assert "invalid project id" in body["error"]

    def test_sessions_unknown_project_returns_400(self):
        status, body = _get_json(self.port, "/api/sessions?project=99")
        assert status == 400
        assert "unknown project id" in body["error"]

    # --- /api/sessions-stat --------------------------------------------------

    def test_sessions_stat_returns_fingerprint(self):
        status, body = _get_json(self.port, "/api/sessions-stat?project=0")
        assert status == 200
        assert isinstance(body, dict)
        assert "mtime" in body
        assert "size" in body
        assert body["mtime"] is not None
        assert body["size"] is not None

    # --- /api/session --------------------------------------------------------

    def test_session_returns_details(self):
        sid = "abc12345-6789-abcd-ef01-234567890abc"
        status, body = _get_json(self.port, f"/api/session?project=0&session={sid}")
        assert status == 200
        assert isinstance(body, dict)
        assert "reqs" in body
        assert "session_meta" in body
        assert body["session_meta"]["session_id"] == sid

    def test_session_missing_session_returns_400(self):
        status, body = _get_json(self.port, "/api/session?project=0")
        assert status == 400
        assert "missing session param" in body["error"]

    # --- /api/session-stat ---------------------------------------------------

    def test_session_stat_returns_fingerprint(self):
        sid = "abc12345-6789-abcd-ef01-234567890abc"
        status, body = _get_json(self.port, f"/api/session-stat?project=0&session={sid}")
        assert status == 200
        assert isinstance(body, dict)
        assert "mtime" in body
        assert "size" in body

    def test_session_stat_missing_session_returns_400(self):
        status, body = _get_json(self.port, "/api/session-stat?project=0")
        assert status == 400
        assert "missing session param" in body["error"]

    # --- /api/groups (unchanged by our fix, but verify not broken) -----------

    def test_groups_returns_list(self):
        status, body = _get_json(self.port, "/api/groups")
        assert status == 200
        assert isinstance(body, list)

    # --- /api/projects (unchanged, but verify not broken) -------------------

    def test_projects_returns_list(self):
        status, body = _get_json(self.port, "/api/projects")
        assert status == 200
        assert isinstance(body, list)
