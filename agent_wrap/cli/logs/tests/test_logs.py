"""
CLI-layer tests for agent_wrap.cli.logs — argument parsing and calling protocol.

``services.logs_service`` is already spec-mocked by the autouse fixture
in ``agent_wrap/cli/conftest.py``.
"""

from __future__ import annotations

import pytest

from agent_wrap.cli.logs import build_parser, run
from agent_wrap.containers import services

# --- arg parsing ---


def test_parse_port_default() -> None:
    assert build_parser().parse_args([]).port == 8765


def test_parse_port_custom() -> None:
    assert build_parser().parse_args(["--port", "9000"]).port == 9000


def test_parse_port_rejects_non_integer(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--port", "abc"])
    assert exc.value.code != 0
    assert "expects an integer" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["0", "70000"])
def test_parse_port_rejects_out_of_range(bad: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--port", bad])
    assert exc.value.code != 0
    assert "must be between" in capsys.readouterr().err


def test_parse_port_help_returns_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["-h"])
    assert exc.value.code == 0


def test_parse_port_unknown_arg(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["--bogus"])
    assert exc.value.code != 0
    assert "unrecognized" in capsys.readouterr().err


def test_parsestop_daemon_flag() -> None:
    assert build_parser().parse_args(["--stop"]).stop is True


# --- run() dispatch -----------------------------------------------------------


def test_runstop_daemon_dispatches_tostop_daemon() -> None:
    """--stop delegates to services.logs_service.stop_daemon()."""
    services.logs_service.stop_daemon.return_value = 0
    assert run(["--stop"]) == 0
    services.logs_service.stop_daemon.assert_called_once_with()  # type: ignore[missing-attribute]


def test_runstop_daemon_rejects_extra_args() -> None:
    """--stop rejects --port (no extra args allowed)."""
    assert run(["--stop", "--port", "9000"]) == 1


def test_run_foreground_dispatches_to_serve_foreground() -> None:
    """--foreground delegates to serve_foreground with the parsed port."""
    services.logs_service.serve_foreground.return_value = 0
    assert run(["--foreground", "--port", "9000"]) == 0
    services.logs_service.serve_foreground.assert_called_once_with(9000)  # type: ignore[missing-attribute]


def test_run_already_running_prints_connect_line_and_skips_spawn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """When a server is already running, print connect line and skip spawn."""
    services.logs_service.running_server.return_value = {"pid": 1, "port": 9123}
    services.logs_service.connect_line.return_value = (
        "LiteLLM log viewer running at http://127.0.0.1:9123"
    )

    assert run(["--port", "8765"]) == 0
    services.logs_service.spawn_background.assert_not_called()  # type: ignore[missing-attribute]
    out = capsys.readouterr().out
    assert out.strip() == "LiteLLM log viewer running at http://127.0.0.1:9123"


def test_run_spawns_when_not_running() -> None:
    """When no server is running, spawn a new background server."""
    services.logs_service.running_server.return_value = None
    services.logs_service.spawn_background.return_value = 0

    assert run(["--port", "9000"]) == 0
    services.logs_service.spawn_background.assert_called_once_with(9000)  # type: ignore[missing-attribute]


def test_run_help_returns_zero() -> None:
    assert run(["-h"]) == 0
