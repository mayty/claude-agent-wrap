# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/sidecars/telegram.py."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
import pytest_mock

from agent_wrap.sidecars.telegram import (
    TelegramSidecar,
    TelegramSidecarConfig,
)

# --- test fixtures ---


def _config(**overrides: object) -> TelegramSidecarConfig:
    """Build a TelegramSidecarConfig with simple defaults."""
    defaults: dict = {
        "image": "agent-wrap-telegram:latest",
        "container_name": "agent-wrap-telegram",
        "network_name": "agent-wrap-net",
        "internal_port": 6837,
        "bot_token": "test-bot-token",
        "chat_id": "test-chat-id",
        "agent_name": "test-agent",
        "instance_id": "test-inst",
        "health_timeout_sec": 30,
        "cold_start_time": 45.0,
        "short_circuit_time": 2.0,
    }
    defaults.update(overrides)
    return TelegramSidecarConfig(**defaults)  # type: ignore[arg-type]


def _sidecar(**overrides: object) -> TelegramSidecar:
    return TelegramSidecar(_config(**overrides))


_DOCKER = "agent_wrap.sidecars.telegram.docker_run"
_IMAGE_EXISTS = "agent_wrap.sidecars.telegram.image_exists"
_URLOPEN = "urllib.request.urlopen"


# --- config / timing ---


def test_config_fields() -> None:
    cfg = _config()
    assert cfg.image == "agent-wrap-telegram:latest"
    assert cfg.container_name == "agent-wrap-telegram"
    assert cfg.network_name == "agent-wrap-net"
    assert cfg.internal_port == 6837
    assert cfg.bot_token == "test-bot-token"
    assert cfg.chat_id == "test-chat-id"
    assert cfg.agent_name == "test-agent"
    assert cfg.instance_id == "test-inst"
    assert cfg.health_timeout_sec == 30
    assert cfg.cold_start_time == 45.0
    assert cfg.short_circuit_time == 2.0


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
    mocker.patch("agent_wrap.sidecars.telegram.get_user_args", return_value=[])
    _sidecar()._start()
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


def test_start_reaps_existing_container(mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    mocker.patch("agent_wrap.sidecars.telegram.get_user_args", return_value=[])
    _sidecar()._start()
    rm_calls = [c.args for c in mock_docker.call_args_list if "rm" in c.args[:2]]
    assert len(rm_calls) == 1


def test_start_failure_raises(mocker: pytest_mock.MockFixture) -> None:
    # First inspect fails (no existing container), then run fails
    mocker.patch(_DOCKER, side_effect=[("", 1), ("", 1)])
    mocker.patch("agent_wrap.sidecars.telegram.get_user_args", return_value=[])
    with pytest.raises(SystemExit):
        _sidecar()._start()


# --- health pollution ---


def test_health_poll_healthy(mocker: pytest_mock.MockFixture) -> None:
    mock_spin = mocker.patch("agent_wrap.sidecars.telegram._SPINNER.poll_until", return_value=True)
    result = _sidecar()._health_poll()
    assert result is True
    mock_spin.assert_called_once()


def test_health_poll_unhealthy(mocker: pytest_mock.MockFixture) -> None:
    mock_spin = mocker.patch("agent_wrap.sidecars.telegram._SPINNER.poll_until", return_value=False)
    result = _sidecar()._health_poll()
    assert result is False
    mock_spin.assert_called_once()


# --- HTTP register / unregister ---


def test_register_success(mocker: pytest_mock.MockFixture) -> None:
    mock_resp = MagicMock()
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
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({}).encode()
    mock_resp.__enter__.return_value = mock_resp
    mocker.patch(_URLOPEN, return_value=mock_resp)

    token = _sidecar()._register()
    assert token == ""


def test_register_http_error(mocker: pytest_mock.MockFixture) -> None:
    import urllib.error

    mocker.patch(_URLOPEN, side_effect=urllib.error.URLError("timeout"))

    token = _sidecar()._register()
    assert token == ""


def test_unregister_sends_auth_header(mocker: pytest_mock.MockFixture) -> None:
    mock_resp = MagicMock()
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
    import urllib.error

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

    result = sc.ensure(use_host_net=False, agent_network=None)

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

    result = sc.ensure(use_host_net=False, agent_network=None)

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
    mocker.patch(_DOCKER)  # for logs call

    with pytest.raises(SystemExit):
        sc.ensure(use_host_net=False, agent_network=None)


def test_ensure_with_custom_agent_network(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_register", return_value="tok-custom")
    mock_attach = mocker.patch.object(sc, "_attach_to_network")
    mocker.patch.object(sc, "_build_connectivity_args", return_value=["-e", "Z=1"])

    sc.ensure(use_host_net=False, agent_network="custom-bridge")

    mock_attach.assert_called_once_with("custom-bridge")


def test_ensure_skips_attach_for_agent_wrap_net(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_register", return_value="tok")
    mock_attach = mocker.patch.object(sc, "_attach_to_network")
    mocker.patch.object(sc, "_build_connectivity_args", return_value=[])

    sc.ensure(use_host_net=False, agent_network="agent-wrap-net")
    mock_attach.assert_not_called()


# --- release ---


def test_release_stops_container(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    sc._auth_token = "tok-rel"
    mocker.patch.object(sc, "_is_running", return_value=True)
    mock_unreg = mocker.patch.object(sc, "_unregister")
    mock_spin = mocker.patch("agent_wrap.sidecars.telegram._SPINNER.spin_while")

    sc.release()

    mock_unreg.assert_called_once()
    mock_spin.assert_called_once()


def test_release_skips_when_not_running(mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar()
    sc._auth_token = "tok-rel"
    mocker.patch.object(sc, "_is_running", return_value=False)
    mock_unreg = mocker.patch.object(sc, "_unregister")
    mock_spin = mocker.patch("agent_wrap.sidecars.telegram._SPINNER.spin_while")

    sc.release()

    mock_unreg.assert_called_once()
    mock_spin.assert_not_called()


def test_release_unregister_called_even_without_token(
    mocker: pytest_mock.MockFixture,
) -> None:
    """Unregister is always called; it's a no-op internally when token is empty."""
    sc = _sidecar()
    sc._auth_token = ""
    mocker.patch.object(sc, "_is_running", return_value=False)
    mock_unreg = mocker.patch.object(sc, "_unregister")

    sc.release()

    mock_unreg.assert_called_once()
