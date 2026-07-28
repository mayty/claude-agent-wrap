# This file has been edited with the assistance of an AI tool.
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.logs.server import bind_port, get_handler, resolve_static
from agent_wrap.domain.pricing.service import PricingService

if TYPE_CHECKING:
    from pathlib import Path


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


def _get_text(port: int, path: str) -> tuple[int, str]:
    """GET *path* and return (status_code, body as text)."""
    req = urllib.request.Request(_url(port, path))  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _get_ndjson(port: int, path: str) -> tuple[int, list[dict[str, Any]]]:
    """GET *path* and return (status_code, list of parsed NDJSON lines)."""
    req = urllib.request.Request(_url(port, path))  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            body = resp.read().decode("utf-8")
            lines: list[dict[str, Any]] = []
            for raw_line in body.split("\n"):
                stripped = raw_line.strip()
                if stripped:
                    lines.append(json.loads(stripped))
            return resp.status, lines
    except urllib.error.HTTPError as e:
        return e.code, [json.loads(e.read())]


def _start_server(port: int, cache: LogsCache) -> threading.Thread:
    """Start the logs HTTP server on *port* in a daemon thread and return the thread."""
    pricing = Mock(spec=PricingService)
    pricing.request_cache_ttl.return_value = None
    pricing.extract_usage.return_value = {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 0,
    }
    pricing.compute_cost.return_value = 0.001
    handler = get_handler(pricing, cache)
    server = bind_port(port, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Give the server a moment to start accepting connections.
    time.sleep(0.1)
    return t


def _write_session(project: Path, provider: str, session_id: str) -> Path:
    """Write a minimal messages.jsonl for *session_id* and return its dir."""
    sdir = project / ".claude" / "litellm-logs" / provider / session_id
    sdir.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "timing": {"start": 1000000.0, "completionStart": None, "end": 1000001.0},
        "model": "m/test",
        "response": {},
    }
    with (sdir / "messages.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    # Write meta.json so stream_session can pick it up from cache.
    meta: dict[str, Any] = {
        "count": 1,
        "last_ts": 1000001.0,
        "models": ["test"],
    }
    (sdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return sdir


@pytest.fixture
def api_server(tmp_path: Path) -> tuple[int, Path]:
    """Start the logs server against a fresh project; return (port, project_dir)."""
    port = _find_free_port()
    project = tmp_path / "testproj"
    sid = "abc12345-6789-abcd-ef01-234567890abc"
    _write_session(project, "litellm-bedrock", sid)
    cache = Mock(spec=LogsCache)
    cache.get_logs_dirs.side_effect = lambda pid: (
        [project / ".claude" / "litellm-logs"] if pid == 0 else None
    )
    cache.get_sessions.return_value = [
        {
            "session_id": sid,
            "count": 1,
            "first_ts": 1000000.0,
            "last_ts": 1000001.0,
            "models": ["test"],
            "providers": ["litellm-bedrock"],
            "alias": None,
            "title": None,
        }
    ]
    cache.get_sessions_fingerprint.return_value = {"mtime": 1, "size": 100}
    cache.get_session_fingerprint.return_value = {"mtime": 1, "size": 100}
    cache.get_hot_session.return_value = None
    cache.get_groups.return_value = []  # type: ignore[implicit-any-empty-container]
    cache.get_projects.return_value = []  # type: ignore[implicit-any-empty-container]
    cache.get_projects_fingerprint.return_value = {"mtime": None, "size": None}  # type: ignore[implicit-any-empty-container]
    _start_server(port, cache)
    return port, project


def test_sessions_returns_list(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/sessions?project=0")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["session_id"] == "abc12345-6789-abcd-ef01-234567890abc"
    assert body[0]["count"] == 1


def test_sessions_missing_project_returns_400(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/sessions")
    assert status == 400
    assert "missing project param" in body["error"]


def test_sessions_invalid_project_returns_400(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/sessions?project=notanumber")
    assert status == 400
    assert "invalid project id" in body["error"]


def test_sessions_unknown_project_returns_400(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/sessions?project=99")
    assert status == 400
    assert "unknown project id" in body["error"]


def test_sessions_stat_returns_fingerprint(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/sessions-stat?project=0")
    assert status == 200
    assert isinstance(body, dict)
    assert "mtime" in body
    assert "size" in body
    assert body["mtime"] is not None
    assert body["size"] is not None


def test_session_returns_details(api_server: tuple[int, Path], subtests: pytest.Subtests):
    port, _project = api_server
    sid = "abc12345-6789-abcd-ef01-234567890abc"
    status, lines = _get_ndjson(port, f"/api/session?project=0&session={sid}")
    assert status == 200
    assert len(lines) >= 2
    # First line is the meta header
    assert lines[0]["__type__"] == "session_meta"
    assert lines[0]["session_id"] == sid
    # Subsequent lines are records (no __type__)
    for i, line in enumerate(lines[1:]):
        with subtests.test(msg=str(i)):  # type: ignore[bad-context-manager]
            assert "__type__" not in line


def test_session_missing_session_returns_400(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/session?project=0")
    assert status == 400
    assert "missing session param" in body["error"]


def test_strings_endpoint_returns_text(api_server: tuple[int, Path]):
    port, project = api_server
    sid = "abc12345-6789-abcd-ef01-234567890abc"
    sdir = _write_session(project, "litellm-bedrock", sid)
    (sdir / "strings.jsonl").write_text('{"hash": "hash:a", "original": "AAA"}\n', encoding="utf-8")
    status, body = _get_text(port, f"/api/strings?project=0&session={sid}")
    assert status == 200
    assert "hash:a" in body
    assert "AAA" in body


def test_strings_endpoint_empty_when_no_file(api_server: tuple[int, Path]):
    port, project = api_server
    sid = "abc12345-6789-abcd-ef01-234567890abc"
    _write_session(project, "litellm-bedrock", sid)
    status, body = _get_text(port, f"/api/strings?project=0&session={sid}")
    assert status == 200
    assert body == ""


def test_session_stat_returns_fingerprint(api_server: tuple[int, Path]):
    port, _project = api_server
    sid = "abc12345-6789-abcd-ef01-234567890abc"
    status, body = _get_json(port, f"/api/session-stat?project=0&session={sid}")
    assert status == 200
    assert isinstance(body, dict)
    assert "mtime" in body
    assert "size" in body


def test_session_stat_missing_session_returns_400(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/session-stat?project=0")
    assert status == 400
    assert "missing session param" in body["error"]


def test_groups_returns_list(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/groups")
    assert status == 200
    assert isinstance(body, list)


def test_projects_returns_list(api_server: tuple[int, Path]):
    port, _project = api_server
    status, body = _get_json(port, "/api/projects")
    assert status == 200
    assert isinstance(body, list)
