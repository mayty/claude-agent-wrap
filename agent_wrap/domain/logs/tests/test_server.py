# This file has been edited with the assistance of an AI tool.
"""
Tests for the viewer's HTTP surface: dispatch, query parameters, and the response.

The server reads nothing itself. Its list endpoints are answered from the cache and its
one content endpoint from :class:`SessionStream`, so both collaborators are mocked here
and the tests are about what the handler does with them — which project id it accepts,
which parameters it refuses, and what it puts on the wire. The stream's own output is
``test_stream.py``, against a real index.
"""

import gzip
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import Mock

import pytest

from agent_wrap.domain.logs.cache import LogsCache
from agent_wrap.domain.logs.server import bind_port, get_handler, resolve_static
from agent_wrap.domain.logs.stream import SessionStream

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

SESSION_ID = "abc12345-6789-abcd-ef01-234567890abc"


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


def _stream_mock(lines: Sequence[str]) -> Mock:
    """
    Return a ``SessionStream`` mock that yields *lines* for any session asked of it.

    A fresh iterator per call: the handler consumes it once per request, and a test that
    makes two requests would otherwise get an empty body for the second.
    """
    stream = Mock(spec=SessionStream)

    def _lines(*_args: object, **_kwargs: object) -> Iterator[str]:
        return iter(lines)

    stream.lines.side_effect = _lines
    return stream


def _start_server(port: int, cache: LogsCache, stream: Mock | None = None) -> threading.Thread:
    """Start the logs HTTP server on *port* in a daemon thread and return the thread."""
    handler = get_handler(stream or _stream_mock([]), cache)
    server = bind_port(port, handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    # Give the server a moment to start accepting connections.
    time.sleep(0.1)
    return t


def _cache_mock() -> Mock:
    """Return a cache mock that knows exactly one project, at id 0."""
    cache = Mock(spec=LogsCache)
    cache.get_project_hashes.side_effect = lambda pid: ["hash0"] if pid == 0 else None  # pyrefly: ignore [implicit-any-lambda]
    cache.get_sessions.return_value = [
        {
            "session_id": SESSION_ID,
            "count": 1,
            "last_ts": 1000001.0,
            "models": ["test"],
            "providers": ["litellm-bedrock"],
            "alias": None,
            "title": None,
        }
    ]
    cache.get_session_meta.return_value = {"session_id": SESSION_ID, "count": 1}
    cache.get_sessions_fingerprint.return_value = {"rev": 1, "count": 1}
    cache.get_session_fingerprint.return_value = {"rev": 1, "count": 1}
    cache.get_projects.return_value = []  # pyrefly: ignore [implicit-any-empty-container]
    cache.get_projects_fingerprint.return_value = {"rev": None, "count": 0}
    return cache


@pytest.fixture
def api_server() -> tuple[int, Mock]:
    """Start the logs server over a one-project cache; return (port, the stream mock)."""
    port = _find_free_port()
    stream = _stream_mock(
        [
            json.dumps({"__type__": "session_meta", "session_id": SESSION_ID, "count": 1}),
            json.dumps({"model": "m/test", "messages": ["blob:1"]}),
        ]
    )
    _start_server(port, _cache_mock(), stream)
    return port, stream


def test_sessions_returns_list(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/sessions?project=0")
    assert status == 200
    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["session_id"] == SESSION_ID
    assert body[0]["count"] == 1


def test_sessions_missing_project_returns_400(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/sessions")
    assert status == 400
    assert "missing project param" in body["error"]


def test_sessions_invalid_project_returns_400(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/sessions?project=notanumber")
    assert status == 400
    assert "invalid project id" in body["error"]


def test_sessions_unknown_project_returns_400(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/sessions?project=99")
    assert status == 400
    assert "unknown project id" in body["error"]


def test_sessions_stat_returns_fingerprint(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/sessions-stat?project=0")
    assert status == 200
    assert body == {"rev": 1, "count": 1}


def test_session_returns_the_stream_as_ndjson(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, lines = _get_ndjson(port, f"/api/session?project=0&session={SESSION_ID}")
    assert status == 200
    assert lines[0] == {"__type__": "session_meta", "session_id": SESSION_ID, "count": 1}
    assert lines[1] == {"model": "m/test", "messages": ["blob:1"]}


def test_session_scopes_the_read_by_the_projects_hashes(api_server: tuple[int, Mock]):
    """
    The hashes, the session id and the cached summary are what the stream is given.

    The hashes are the join with the index: passing the wrong ones would serve one
    project's session content under another project's id.
    """
    port, stream = api_server
    _get_ndjson(port, f"/api/session?project=0&session={SESSION_ID}")
    hashes, session_id, meta = stream.lines.call_args.args
    assert hashes == ["hash0"]
    assert session_id == SESSION_ID
    assert meta == {"session_id": SESSION_ID, "count": 1}


def test_session_defaults_to_the_whole_session(api_server: tuple[int, Mock]):
    port, stream = api_server
    _get_ndjson(port, f"/api/session?project=0&session={SESSION_ID}")
    assert stream.lines.call_args.kwargs == {"from_index": 0, "limit": None}


def test_session_passes_from_and_limit_through(api_server: tuple[int, Mock]):
    port, stream = api_server
    _get_ndjson(port, f"/api/session?project=0&session={SESSION_ID}&from=7&limit=3")
    assert stream.lines.call_args.kwargs == {"from_index": 7, "limit": 3}


@pytest.mark.parametrize(("name", "value"), [("from", "-1"), ("from", "x"), ("limit", "-4")])
def test_session_refuses_an_unusable_bound(api_server: tuple[int, Mock], name: str, value: str):
    """A negative bound would silently slice from the end of the session instead."""
    port, stream = api_server
    status, body = _get_json(port, f"/api/session?project=0&session={SESSION_ID}&{name}={value}")
    assert status == 400
    assert f"invalid {name} value" in body["error"]
    stream.lines.assert_not_called()


def test_session_missing_session_returns_400(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/session?project=0")
    assert status == 400
    assert "missing session param" in body["error"]


def test_the_strings_endpoint_is_gone(api_server: tuple[int, Mock]):
    """
    Interned strings travel on the session stream now, so the endpoint has no subject.

    Asserted rather than merely deleted: an unrouted path falls through to the static
    handler, and a 404 is what tells an old cached page to stop asking.
    """
    port, _stream = api_server
    req = urllib.request.Request(  # noqa: S310
        _url(port, f"/api/strings?project=0&session={SESSION_ID}")
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    assert excinfo.value.code == 404


def test_session_stat_returns_fingerprint(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, f"/api/session-stat?project=0&session={SESSION_ID}")
    assert status == 200
    assert body == {"rev": 1, "count": 1}


def test_a_project_with_nothing_indexed_still_answers_with_a_fingerprint():
    """
    The empty answer has to be a fingerprint, not an error and not an omission.

    The browser polls both ``*-stat`` endpoints for whatever it has open, including a
    project whose sessions are not in the index yet, and ``fpKey`` has to make a stable
    comparison key out of the reply. An error body would compare equal to itself too --
    and then never move once the sessions did arrive.
    """
    port = _find_free_port()
    cache = Mock(spec=LogsCache)
    cache.get_project_hashes.side_effect = lambda pid: (  # pyrefly: ignore [implicit-any-lambda]
        cast("list[str]", []) if pid == 0 else None
    )
    cache.get_sessions_fingerprint.return_value = None
    cache.get_session_fingerprint.return_value = None
    _start_server(port, cache)

    assert _get_json(port, "/api/sessions-stat?project=0") == (200, {"rev": None, "count": 0})
    assert _get_json(port, "/api/session-stat?project=0&session=s1") == (
        200,
        {"rev": None, "count": 0},
    )


def test_session_stat_missing_session_returns_400(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/session-stat?project=0")
    assert status == 400
    assert "missing session param" in body["error"]


def test_projects_returns_list(api_server: tuple[int, Mock]):
    port, _stream = api_server
    status, body = _get_json(port, "/api/projects")
    assert status == 200
    assert isinstance(body, list)


# Enough records that the body clears GZIP_MIN_BYTES, and repetitive enough that gzip
# actually shrinks it -- which is what this endpoint carries in production.
BIG_RECORD_COUNT = 200


def _get_raw(port: int, path: str, accept_encoding: str | None = None) -> tuple[int, str, bytes]:
    """
    GET *path* and return ``(status, Content-Encoding, raw body)``.

    ``urlopen`` does not decompress on its own and sends no ``Accept-Encoding`` unless
    one is set here, so the body comes back exactly as the server wrote it.
    """
    req = urllib.request.Request(_url(port, path))  # noqa: S310
    if accept_encoding is not None:
        req.add_header("Accept-Encoding", accept_encoding)
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        return resp.status, resp.headers.get("Content-Encoding", ""), resp.read()


@pytest.fixture
def gzip_server() -> int:
    """Start a server whose session stream is long enough to be worth compressing."""
    port = _find_free_port()
    lines = [json.dumps({"__type__": "session_meta", "count": BIG_RECORD_COUNT})]
    lines.extend(
        json.dumps({"i": i, "text": "the quick brown fox " * 20}) for i in range(BIG_RECORD_COUNT)
    )
    _start_server(port, _cache_mock(), _stream_mock(lines))
    return port


def test_session_is_gzipped_when_the_client_accepts_it(gzip_server: int):
    status, encoding, body = _get_raw(
        gzip_server, f"/api/session?project=0&session={SESSION_ID}", "gzip"
    )
    assert status == 200
    assert encoding == "gzip"
    decoded = gzip.decompress(body).decode("utf-8")
    # Round-trips to the NDJSON the client would otherwise have received.
    lines = [json.loads(line) for line in decoded.strip().split("\n")]
    assert len(lines) == BIG_RECORD_COUNT + 1
    assert lines[0]["__type__"] == "session_meta"
    assert len(body) < len(decoded.encode("utf-8"))


def test_session_is_not_gzipped_without_accept_encoding(gzip_server: int):
    status, encoding, body = _get_raw(gzip_server, f"/api/session?project=0&session={SESSION_ID}")
    assert status == 200
    assert encoding == ""
    # Plain UTF-8 NDJSON, not a gzip member.
    assert body.decode("utf-8").startswith('{"__type__": "session_meta"')


def test_small_body_is_not_gzipped_even_when_accepted(api_server: tuple[int, Mock]):
    """A body below GZIP_MIN_BYTES is sent as-is -- the header would outweigh the saving."""
    port, _stream = api_server
    status, encoding, body = _get_raw(port, f"/api/session?project=0&session={SESSION_ID}", "gzip")
    assert status == 200
    assert encoding == ""
    assert len(body) < 1024


def test_content_length_matches_the_compressed_body(gzip_server: int):
    """The header must describe the bytes on the wire, not the bytes before compression."""
    req = urllib.request.Request(  # noqa: S310
        _url(gzip_server, f"/api/session?project=0&session={SESSION_ID}")
    )
    req.add_header("Accept-Encoding", "gzip")
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
        declared = int(resp.headers["Content-Length"])
        body = resp.read()
    assert declared == len(body)


@pytest.mark.parametrize(
    ("accept_encoding", "expected"),
    [
        ("gzip", "gzip"),
        ("gzip, deflate, br", "gzip"),
        ("deflate, gzip;q=0.8", "gzip"),
        ("  GZIP  ", "gzip"),
        ("deflate", ""),
        ("x-gzip", ""),
        ("", ""),
    ],
)
def test_accept_encoding_is_matched_by_token(gzip_server: int, accept_encoding: str, expected: str):
    """Only a real ``gzip`` token counts -- not a substring of some other encoding name."""
    _status, encoding, _body = _get_raw(
        gzip_server, f"/api/session?project=0&session={SESSION_ID}", accept_encoding
    )
    assert encoding == expected
