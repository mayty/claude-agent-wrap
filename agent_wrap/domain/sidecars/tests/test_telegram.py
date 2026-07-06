# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/sidecars/telegram.py."""

from __future__ import annotations

import json
import tempfile
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.sidecars.models import TelegramSidecarConfig
from agent_wrap.domain.sidecars.telegram import TelegramSidecar

if TYPE_CHECKING:
    import pytest_mock

# --- test fixtures ---

_TEST_LOG_DIR = Path(tempfile.gettempdir(), "test-tg-logs")


def _config(**overrides: object) -> TelegramSidecarConfig:
    """Build a TelegramSidecarConfig with simple defaults."""
    defaults: dict[str, Any] = {
        "image": (
            "mayty/claude-agent-wrap-telegram:0.1.0"
            "@sha256:73c39566944046389ebd3bad89d1e4d6c2afe545f641edc74e0e08914c41d4bf"
        ),
        "container_name": "agent-wrap-telegram",
        "network_name": "agent-wrap-net",
        "internal_port": 6837,
        "agent_name": "test-agent",
        "instance_id": "test-inst",
        "health_timeout_sec": 30,
        "cold_start_time": 45.0,
        "short_circuit_time": 2.0,
        "log_dir": _TEST_LOG_DIR,
    }
    defaults.update(overrides)
    return TelegramSidecarConfig(**defaults)  # type: ignore[arg-type]


def _sidecar(display: DisplayService | None = None, **overrides: object) -> TelegramSidecar:
    if display is None:
        display = Mock(spec=DisplayService)
    return TelegramSidecar(_config(**overrides), display_service=display)


_SECRETS = {"TelegramBotToken": "test-bot-token", "TelegramChatId": "test-chat-id"}
_DOCKER = "agent_wrap.domain.sidecars.telegram.docker_run"
_IMAGE_EXISTS = "agent_wrap.domain.sidecars.telegram.image_exists"
_URLOPEN = "urllib.request.urlopen"


# --- config / timing ---


def test_config_fields() -> None:
    cfg = _config()
    assert cfg.image == (
        "mayty/claude-agent-wrap-telegram:0.1.0"
        "@sha256:73c39566944046389ebd3bad89d1e4d6c2afe545f641edc74e0e08914c41d4bf"
    )
    assert cfg.container_name == "agent-wrap-telegram"
    assert cfg.network_name == "agent-wrap-net"
    assert cfg.internal_port == 6837
    assert cfg.agent_name == "test-agent"
    assert cfg.instance_id == "test-inst"
    assert cfg.health_timeout_sec == 30
    assert cfg.cold_start_time == 45.0
    assert cfg.short_circuit_time == 2.0
    assert cfg.log_dir == _TEST_LOG_DIR


def test_timing() -> None:
    sc = _sidecar()
    assert sc.cold_start_time == 45.0
    assert sc.short_circuit_time == 2.0


# --- prepare ---


def test_prepare_image_exists(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_IMAGE_EXISTS, return_value=True)
    mock_docker = mocker.patch(_DOCKER)
    _sidecar().prepare()
    mock_docker.assert_not_called()


def test_prepare_pulls_missing_image(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_IMAGE_EXISTS, return_value=False)
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    _sidecar().prepare()
    assert any("pull" in str(c) for c in mock_docker.call_args_list)


def test_prepare_pull_failure_raises(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_IMAGE_EXISTS, return_value=False)
    mocker.patch(_DOCKER, return_value=("", 1))
    with pytest.raises(SystemExit):
        _sidecar().prepare()


# --- headless: prepare/ensure are no-ops, release still reaps ---


def test_prepare_noop_when_headless(mocker: pytest_mock.MockFixture) -> None:
    """A headless run never pulls the image — prepare() touches nothing."""
    mock_exists = mocker.patch(_IMAGE_EXISTS)
    mock_docker = mocker.patch(_DOCKER)
    _sidecar(headless=True).prepare()
    mock_exists.assert_not_called()
    mock_docker.assert_not_called()


def test_ensure_noop_when_headless(mocker: pytest_mock.MockFixture) -> None:
    """A headless run never starts/registers — ensure() returns no agent flags."""
    mock_docker = mocker.patch(_DOCKER)
    sc = _sidecar(headless=True)
    mock_net = mocker.patch.object(sc, "_ensure_network")
    mock_start = mocker.patch.object(sc, "_start")
    mock_reg = mocker.patch.object(sc, "_register")

    result = sc.ensure(use_host_net=False, agent_network=None, secrets=_SECRETS)

    assert result == []
    mock_net.assert_not_called()
    mock_start.assert_not_called()
    mock_reg.assert_not_called()
    mock_docker.assert_not_called()


def test_release_still_stops_running_container_when_headless(
    mocker: pytest_mock.MockFixture,
) -> None:
    """
    A headless run that is last-out must still reap the shared singleton.

    Regression guard: release() is not gated on headless.
    """
    sc = _sidecar(headless=True)
    mocker.patch.object(sc, "_is_running", return_value=True)
    mock_spin = mocker.patch.object(sc._display, "spin_while")

    sc.release()

    mock_spin.assert_called_once()


# --- docker helpers ---


def test_is_running_true(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("true", 0))
    assert _sidecar()._is_running() is True


def test_is_running_false(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("false", 0))
    assert _sidecar()._is_running() is False


def test_is_running_error(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("", 1))
    assert _sidecar()._is_running() is False


def test_is_on_network_true(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("agent-wrap-net\ncustom-net\n", 0))
    sc = _sidecar()
    assert sc._is_on_network("custom-net") is True


def test_is_on_network_false(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("agent-wrap-net\n", 0))
    sc = _sidecar()
    assert sc._is_on_network("custom-net") is False


def test_is_on_network_error(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("", 1))
    sc = _sidecar()
    assert sc._is_on_network("agent-wrap-net") is False


def test_ensure_network_exists(mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    _sidecar()._ensure_network()
    calls = [c.args for c in mock_docker.call_args_list]
    assert any("inspect" in c for c in calls)


def test_ensure_network_creates(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, side_effect=[("", 1), ("", 0)])
    _sidecar()._ensure_network()


def test_ensure_network_create_fails(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, side_effect=[("", 1), ("", 1)])
    with pytest.raises(SystemExit):
        _sidecar()._ensure_network()


# --- attach_to_network ---


def test_attach_to_network_missing_raises(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, side_effect=[("", 1)])  # network inspect fails
    with pytest.raises(SystemExit):
        _sidecar()._attach_to_network("missing-net")


def test_attach_to_network_already_connected(mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    # First call: network inspect (exists)
    # Second: is_on_network (already connected)
    sc = _sidecar()
    mocker.patch.object(sc, "_is_on_network", return_value=True)
    sc._attach_to_network("custom-net")
    # Only network inspect was called; connect was skipped
    assert not any("connect" in str(c) for c in mock_docker.call_args_list)


def test_attach_to_network_connects(mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    sc = _sidecar()
    mocker.patch.object(sc, "_is_on_network", return_value=False)
    sc._attach_to_network("custom-net")
    assert any("connect" in str(c) for c in mock_docker.call_args_list)


def test_attach_to_network_connect_fails(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_is_on_network", return_value=False)
    # First: network inspect ok, then connect fails
    mocker.patch(_DOCKER, side_effect=[("", 0), ("", 1)])
    with pytest.raises(SystemExit):
        sc._attach_to_network("custom-net")


# --- start ---


def test_start_structure(mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    mocker.patch("agent_wrap.domain.sidecars.telegram.get_user_args", return_value=[])
    sc = _sidecar()
    sc._bot_token = "test-bot-token"
    sc._chat_id = "test-chat-id"
    sc._start()
    # Find the 'run' command in the call list
    run_calls = [c.args for c in mock_docker.call_args_list if "run" in c.args[:2]]
    assert len(run_calls) == 1
    args = run_calls[0]
    assert "agent-wrap-telegram" in args
    assert "agent-wrap-net" in args
    assert "TELEGRAM_BOT_TOKEN=test-bot-token" in args
    assert "TELEGRAM_CHAT_ID=test-chat-id" in args
    assert "-p" in args
    assert "127.0.0.1:6837:6837" in args
    # Volume mount for logs
    assert "-v" in args
    v_idx = args.index("-v")
    assert args[v_idx + 1] == f"{_TEST_LOG_DIR}:/var/log/telegram-sidecar"
    # LOG_LOCATION env var should be present (timestamp varies)
    log_loc_args = [a for a in args if a.startswith("LOG_LOCATION=")]
    assert len(log_loc_args) == 1
    assert log_loc_args[0].startswith("LOG_LOCATION=/var/log/telegram-sidecar/")
    assert log_loc_args[0].endswith(".log")


def test_start_reaps_existing_container(mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    mocker.patch("agent_wrap.domain.sidecars.telegram.get_user_args", return_value=[])
    sc = _sidecar()
    sc._bot_token = "x"
    sc._chat_id = "x"
    sc._start()
    rm_calls = [c.args for c in mock_docker.call_args_list if "rm" in c.args[:2]]
    assert len(rm_calls) == 1


def test_start_failure_raises(mocker: pytest_mock.MockFixture) -> None:
    # First inspect fails (no existing container), then run fails
    mocker.patch(_DOCKER, side_effect=[("", 1), ("", 1)])
    mocker.patch("agent_wrap.domain.sidecars.telegram.get_user_args", return_value=[])
    with pytest.raises(SystemExit):
        _sidecar()._start()


def test_start_creates_log_dir(mocker: pytest_mock.MockFixture, tmp_path: Path) -> None:
    log_dir = tmp_path / "tg-logs"
    assert not log_dir.exists()
    mocker.patch(_DOCKER, return_value=("", 0))
    mocker.patch("agent_wrap.domain.sidecars.telegram.get_user_args", return_value=[])
    _sidecar(log_dir=log_dir)._start()
    assert log_dir.exists()
    assert log_dir.is_dir()


# --- health pollution ---


def test_health_poll_healthy(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mock_spin = mocker.patch.object(sc._display, "poll_until", return_value=True)
    result = sc._health_poll()
    assert result is True
    mock_spin.assert_called_once()


def test_health_poll_unhealthy(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mock_spin = mocker.patch.object(sc._display, "poll_until", return_value=False)
    result = sc._health_poll()
    assert result is False
    mock_spin.assert_called_once()


# --- HTTP register / unregister ---


def test_register_success(mocker: pytest_mock.MockFixture) -> None:
    # urlopen response mock — needs dunder methods for context-manager
    # protocol, which spec= lists cannot provide. MagicMock is used
    # intentionally here.
    # MagicMock needed for context-manager dunder support (__enter__/__exit__).
    mock_resp = mocker.MagicMock()
    mock_resp.read.return_value = json.dumps({"auth_token": "tok-abc-123"}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen = mocker.patch(_URLOPEN, return_value=mock_resp)

    token = _sidecar()._register()
    assert token == "tok-abc-123"

    # Verify the payload sent to the sidecar
    req = mock_urlopen.call_args[0][0]
    sent_body = json.loads(req.data)
    assert sent_body["agent_id"] == "test-inst"
    assert sent_body["agent_name"] == "test-agent"


def test_register_missing_token(mocker: pytest_mock.MockFixture) -> None:
    # urlopen response mock — needs dunder methods for context-manager
    # protocol, which spec= lists cannot provide. MagicMock is used
    # intentionally here.
    # MagicMock needed for context-manager dunder support (__enter__/__exit__).
    mock_resp = mocker.MagicMock()
    mock_resp.read.return_value = json.dumps({}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mocker.patch(_URLOPEN, return_value=mock_resp)

    token = _sidecar()._register()
    assert token == ""


def test_register_http_error(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_URLOPEN, side_effect=urllib.error.URLError("timeout"))

    token = _sidecar()._register()
    assert token == ""


def test_unregister_sends_auth_header(mocker: pytest_mock.MockFixture) -> None:
    # urlopen response mock — needs dunder methods for context-manager
    # protocol, which spec= lists cannot provide. MagicMock is used
    # intentionally here.
    # MagicMock needed for context-manager dunder support (__enter__/__exit__).
    mock_resp = mocker.MagicMock()
    mock_resp.__enter__.return_value = mock_resp
    mock_urlopen = mocker.patch(_URLOPEN, return_value=mock_resp)

    sc = _sidecar()
    sc._auth_token = "tok-xyz"
    sc._unregister()

    mock_urlopen.assert_called_once()
    req = mock_urlopen.call_args[0][0]
    assert req.get_header("Authorization") == "Bearer tok-xyz"


def test_unregister_no_token_skips(mocker: pytest_mock.MockFixture) -> None:
    mock_urlopen = mocker.patch(_URLOPEN)
    _sidecar()._unregister()
    mock_urlopen.assert_not_called()


def test_unregister_swallows_errors(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_URLOPEN, side_effect=urllib.error.URLError("timeout"))
    sc = _sidecar()
    sc._auth_token = "tok-err"
    sc._unregister()  # Should not raise


# --- connectivity args ---


def test_connectivity_args_basic() -> None:
    sc = _sidecar()
    sc._auth_token = ""
    args = sc._build_connectivity_args(agent_in_host_netns=False)
    assert "-e" in args
    url_idx = args.index("-e")
    assert args[url_idx + 1] == "TELEGRAM_SIDECAR_URL=http://agent-wrap-telegram:6837"
    assert "TELEGRAM_SIDECAR_TOKEN" not in " ".join(args)


def test_connectivity_args_with_token() -> None:
    sc = _sidecar()
    sc._auth_token = "tok-123"
    args = sc._build_connectivity_args(agent_in_host_netns=False)
    flat = " ".join(args)
    assert "TELEGRAM_SIDECAR_URL=http://agent-wrap-telegram:6837" in flat
    assert "TELEGRAM_SIDECAR_TOKEN=tok-123" in flat


def test_connectivity_args_host_netns(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    sc._auth_token = "tok-456"
    mocker.patch.object(sc, "_sidecar_ip_on_network", return_value="172.20.0.3")
    args = sc._build_connectivity_args(agent_in_host_netns=True)
    assert "--add-host" in args
    assert "agent-wrap-telegram:172.20.0.3" in args


# --- ensure full flow ---


def test_ensure_full_flow(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mock_net = mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=False)
    mock_start = mocker.patch.object(sc, "_start")
    mock_health = mocker.patch.object(sc, "_health_poll", return_value=True)
    mock_reg = mocker.patch.object(sc, "_register", return_value="tok-full")
    mock_attach = mocker.patch.object(sc, "_attach_to_network")
    mocker.patch.object(sc, "_build_connectivity_args", return_value=["-e", "FOO=bar"])

    result = sc.ensure(use_host_net=False, agent_network=None, secrets=_SECRETS)

    mock_net.assert_called_once()
    mock_start.assert_called_once()
    mock_health.assert_called_once()
    mock_reg.assert_called_once()
    mock_attach.assert_not_called()  # no custom network
    assert result == ["-e", "FOO=bar"]
    assert sc._auth_token == "tok-full"


def test_ensure_already_running(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mock_start = mocker.patch.object(sc, "_start")
    mock_health = mocker.patch.object(sc, "_health_poll")
    mock_reg = mocker.patch.object(sc, "_register", return_value="tok-hot")
    mocker.patch.object(sc, "_attach_to_network")
    mocker.patch.object(sc, "_build_connectivity_args", return_value=["-e", "X=1"])

    result = sc.ensure(use_host_net=False, agent_network=None, secrets=_SECRETS)

    mock_start.assert_not_called()
    mock_health.assert_not_called()
    mock_reg.assert_called_once()
    assert result == ["-e", "X=1"]
    assert sc._auth_token == "tok-hot"


def test_ensure_health_fail_raises(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=False)
    mocker.patch.object(sc, "_start")
    mocker.patch.object(sc, "_health_poll", return_value=False)
    mock_docker = mocker.patch(_DOCKER)  # for logs + rm calls

    with pytest.raises(SystemExit):
        sc.ensure(use_host_net=False, agent_network=None, secrets=_SECRETS)

    # Logs must stream straight through (capture=False) so a startup
    # traceback on the container's stderr reaches the user, and the stopped
    # container (started without --rm) is reaped afterwards.
    logs_calls = [c for c in mock_docker.call_args_list if "logs" in c.args[:1]]
    assert len(logs_calls) == 1
    assert logs_calls[0].kwargs.get("capture") is False
    # Teardown is graceful stop-then-remove (no SIGKILL): a `stop` and a plain
    # `rm` (no -f) so the sidecar's in-container cleanup stage runs.
    stop_calls = [c for c in mock_docker.call_args_list if "stop" in c.args[:1]]
    rm_calls = [c for c in mock_docker.call_args_list if "rm" in c.args[:1]]
    assert len(stop_calls) == 1
    assert len(rm_calls) == 1
    assert "-f" not in rm_calls[0].args


def test_ensure_health_fail_reaps_even_if_log_stream_raises(
    mocker: pytest_mock.MockFixture,
) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=False)
    mocker.patch.object(sc, "_start")
    mocker.patch.object(sc, "_health_poll", return_value=False)

    # Streaming the logs writes to a closed stderr/pipe and raises. Teardown must
    # still run (it's in a `finally`); the launch aborts with the raised error
    # rather than the trailing SystemExit, which is fine — either way it aborts.
    def docker_side_effect(*args: str, **_kwargs: object) -> tuple[str, int]:
        if args[:1] == ("logs",):
            raise BrokenPipeError
        return "", 0

    mock_docker = mocker.patch(_DOCKER, side_effect=docker_side_effect)

    with pytest.raises(BrokenPipeError):
        sc.ensure(use_host_net=False, agent_network=None, secrets=_SECRETS)

    stop_calls = [c for c in mock_docker.call_args_list if "stop" in c.args[:1]]
    rm_calls = [c for c in mock_docker.call_args_list if "rm" in c.args[:1]]
    assert len(stop_calls) == 1
    assert len(rm_calls) == 1
    assert rm_calls[0].args == ("rm", "agent-wrap-telegram")


def test_ensure_with_custom_agent_network(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_register", return_value="tok-custom")
    mock_attach = mocker.patch.object(sc, "_attach_to_network")
    mocker.patch.object(sc, "_build_connectivity_args", return_value=["-e", "Z=1"])

    sc.ensure(use_host_net=False, agent_network="custom-bridge", secrets=_SECRETS)

    mock_attach.assert_called_once_with("custom-bridge")


def test_ensure_skips_attach_for_agent_wrap_net(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_register", return_value="tok")
    mock_attach = mocker.patch.object(sc, "_attach_to_network")
    mocker.patch.object(sc, "_build_connectivity_args", return_value=[])

    sc.ensure(use_host_net=False, agent_network="agent-wrap-net", secrets=_SECRETS)
    mock_attach.assert_not_called()


# --- release ---


def test_release_stops_container(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    sc._auth_token = "tok-rel"
    mocker.patch.object(sc, "_is_running", return_value=True)
    mock_unreg = mocker.patch.object(sc, "_unregister")
    # Run the work lambda inline so we observe the actual docker call: teardown
    # must `rm -f` (the container has no --rm), not merely `stop`.
    mock_spin = mocker.patch.object(
        sc._display,
        "spin_while",
        side_effect=lambda *, work, **_: work(),
    )
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))

    sc.release()

    mock_unreg.assert_not_called()
    mock_spin.assert_called_once()
    # Graceful teardown: stop (SIGTERM) then plain rm (no -f/SIGKILL), in order.
    assert [c.args for c in mock_docker.call_args_list] == [
        ("stop", "agent-wrap-telegram"),
        ("rm", "agent-wrap-telegram"),
    ]


def test_release_skips_when_not_running(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    sc._auth_token = "tok-rel"
    mocker.patch.object(sc, "_is_running", return_value=False)
    mock_unreg = mocker.patch.object(sc, "_unregister")
    mock_spin = mocker.patch.object(sc._display, "spin_while")

    sc.release()

    mock_unreg.assert_not_called()
    mock_spin.assert_not_called()


def test_on_exit_unregister_called_even_without_token(
    mocker: pytest_mock.MockFixture,
) -> None:
    """Unregister is always called via on_exit; it's a no-op internally when token is empty."""
    sc = _sidecar()
    sc._auth_token = ""
    mock_unreg = mocker.patch.object(sc, "_unregister")

    sc.on_exit()

    mock_unreg.assert_called_once()


def test_on_exit_calls_unregister(mocker: pytest_mock.MockFixture) -> None:
    """on_exit delegates to _unregister to tear down the per-agent session."""
    sc = _sidecar()
    sc._auth_token = "tok-exit"
    mock_unreg = mocker.patch.object(sc, "_unregister")

    sc.on_exit()

    mock_unreg.assert_called_once()
