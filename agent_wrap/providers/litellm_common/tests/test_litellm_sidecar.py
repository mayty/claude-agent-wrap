# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/litellm_sidecar.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.lib.utils import project_path_hash
from agent_wrap.providers.litellm_common.litellm_sidecar import (
    LiteLLMSidecar,
    LiteLLMSidecarConfig,
)

# --- test fixtures ---


def _config(tmp_path: Path, **overrides: object) -> LiteLLMSidecarConfig:
    """Build a LiteLLMSidecarConfig with simple hooks, rooted at *tmp_path*."""
    defaults: dict = {
        "image": "test-image:latest",
        "container_name": "agent-wrap-litellm",
        "network_name": "agent-wrap-net",
        "internal_port": 4000,
        "master_key_prefix": "sk-test-",
        "provider_name": "litellm-test",
        "health_timeout_sec": 90,
        "health_endpoint": "/health/liveliness",
        "cold_start_time": 300.0,
        "short_circuit_time": 30.0,
        "config_path": tmp_path / "config.yaml",
        "callback_dir": tmp_path / "callbacks",
        "log_dir": tmp_path / "logs",
        "get_sidecar_env": lambda secrets: {"UPSTREAM_KEY": secrets.get("_secret_key", "")},
        "get_agent_env": lambda master_key, base_url: {"API_KEY": master_key, "BASE_URL": base_url},
        "read_secret_key": lambda secrets: secrets.get("_secret_key", ""),
        "get_sidecar_cmd_args": list,
        "on_started": lambda _key: None,
        "on_stopping": lambda _key: None,
    }
    defaults.update(overrides)
    return LiteLLMSidecarConfig(**defaults)  # type: ignore[arg-type]


def _sidecar(tmp_path: Path, **overrides: object) -> LiteLLMSidecar:
    return LiteLLMSidecar(_config(tmp_path, **overrides))


_DOCKER = "agent_wrap.providers.litellm_common.litellm_sidecar.docker_run"
_IMAGE_EXISTS = "agent_wrap.providers.litellm_common.litellm_sidecar.image_exists"


# --- identity / timing ---


def test_timing(tmp_path: Path) -> None:
    sc = _sidecar(tmp_path)
    assert sc.cold_start_time == 300.0
    assert sc.short_circuit_time == 30.0


def test_generate_master_key(tmp_path: Path) -> None:
    key = _sidecar(tmp_path)._generate_master_key()
    assert key.startswith("sk-test-")
    assert "-" not in key.removeprefix("sk-test-")


# --- docker-dependent helpers ---


def test_is_running_true(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("true", 0))
    assert _sidecar(tmp_path)._is_running() is True


def test_is_running_false(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("false", 0))
    assert _sidecar(tmp_path)._is_running() is False


def test_is_running_error(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("", 1))
    assert _sidecar(tmp_path)._is_running() is False


def test_is_on_network_true(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("agent-wrap-net\nhost\n", 0))
    assert _sidecar(tmp_path)._is_on_network("host") is True


def test_is_on_network_false(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("agent-wrap-net\n", 0))
    assert _sidecar(tmp_path)._is_on_network("host") is False


def test_ensure_network_exists(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    _sidecar(tmp_path)._ensure_network()
    calls = [c.args for c in mock_docker.call_args_list]
    assert any("network" in c and "inspect" in c for c in calls)


def test_ensure_network_creates(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, side_effect=[("", 1), ("", 0)])
    _sidecar(tmp_path)._ensure_network()


def test_ensure_network_create_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, side_effect=[("", 1), ("", 1)])
    with pytest.raises(SystemExit):
        _sidecar(tmp_path)._ensure_network()


def test_attach_to_network_already_connected(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_DOCKER, return_value=("", 0))
    mocker.patch.object(sc, "_is_on_network", return_value=True)
    sc._attach_to_network("mynet")


def test_sidecar_ip_on_network(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("172.18.0.2", 0))
    assert _sidecar(tmp_path)._sidecar_ip_on_network("agent-wrap-net") == "172.18.0.2"


def test_sidecar_ip_on_network_failure(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("", 1))
    assert _sidecar(tmp_path)._sidecar_ip_on_network("agent-wrap-net") == ""


# --- _build_connectivity_args (the 5-case matrix) ---


def test_connectivity_host_sidecar_host_agent(tmp_path: Path) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    result = sc._build_connectivity_args("host", agent_in_host_netns=True, agent_network=None)
    assert "--add-host" in result
    assert "agent-wrap-litellm:127.0.0.1" in result


def test_connectivity_host_sidecar_bridge_agent(tmp_path: Path) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    result = sc._build_connectivity_args("host", agent_in_host_netns=False, agent_network=None)
    assert "agent-wrap-litellm:host-gateway" in result


def test_connectivity_bridge_sidecar_host_netns_agent(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    mocker.patch.object(sc, "_sidecar_ip_on_network", return_value="172.18.0.2")
    result = sc._build_connectivity_args("bridge", agent_in_host_netns=True, agent_network=None)
    assert "agent-wrap-litellm:172.18.0.2" in result


def test_connectivity_bridge_sidecar_host_netns_no_ip_raises(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    mocker.patch.object(sc, "_sidecar_ip_on_network", return_value="")
    with pytest.raises(SystemExit, match="no IP"):
        sc._build_connectivity_args("bridge", agent_in_host_netns=True, agent_network=None)


def test_connectivity_bridge_sidecar_no_agent_network(tmp_path: Path) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    result = sc._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network=None)
    assert "--network" in result
    assert "agent-wrap-net" in result


def test_connectivity_bridge_sidecar_custom_agent_network(tmp_path: Path) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    result = sc._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network="mynet")
    assert "--network" not in result
    assert "-e" in result


# --- log-prefix header injection ---


def test_connectivity_injects_log_prefix_header(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    cwd = Path("/some/project")
    mocker.patch("agent_wrap.providers.litellm_common.litellm_sidecar.Path.cwd", return_value=cwd)
    result = sc._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network="mynet")
    expected = f"ANTHROPIC_CUSTOM_HEADERS=x-agent-wrap-log-prefix: {project_path_hash(cwd)}"
    assert expected in result


def test_connectivity_merges_existing_custom_header(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(
        tmp_path,
        get_agent_env=lambda _master_key, _base_url: {
            "ANTHROPIC_CUSTOM_HEADERS": "x-foo: bar",
        },
    )
    sc._master_key = "sk-test-abc"
    cwd = Path("/some/project")
    mocker.patch("agent_wrap.providers.litellm_common.litellm_sidecar.Path.cwd", return_value=cwd)
    result = sc._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network="mynet")
    value = f"x-foo: bar\nx-agent-wrap-log-prefix: {project_path_hash(cwd)}"
    assert f"ANTHROPIC_CUSTOM_HEADERS={value}" in result


# --- ensure (happy path) ---


def test_ensure_returns_connectivity_args(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc, "_ensure_image")
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_is_on_network", return_value=False)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-recovered")
    result = sc.ensure(use_host_net=False, agent_network=None)
    # Returns docker run flags (env + connectivity), including the log header.
    assert "-e" in result
    assert any("ANTHROPIC_CUSTOM_HEADERS=" in a for a in result)


def test_ensure_existing_sidecar_does_not_reapprove(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    started = mocker.MagicMock()
    sc = _sidecar(tmp_path, on_started=started)
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_is_on_network", return_value=False)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-recovered")
    sc.ensure(use_host_net=False, agent_network=None)
    assert sc._master_key == "sk-test-recovered"
    started.assert_not_called()


def test_ensure_first_launch_approves_once(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    started = mocker.MagicMock()
    sc = _sidecar(tmp_path, on_started=started)
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=False)
    mocker.patch.object(sc, "_load_secrets", return_value={"_secret_key": "k"})
    mocker.patch.object(sc, "_generate_master_key", return_value="sk-test-new")
    mocker.patch.object(sc, "_start")
    mocker.patch.object(sc, "_health_poll", return_value=True)
    sc.ensure(use_host_net=False, agent_network=None)
    assert sc._master_key == "sk-test-new"
    started.assert_called_once_with("sk-test-new")


def test_prepare_pulls_image(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """prepare() pulls the image (lock-free pre-work) and does nothing else."""
    sc = _sidecar(tmp_path)
    ensure_image = mocker.patch.object(sc, "_ensure_image")
    ensure_network = mocker.patch.object(sc, "_ensure_network")
    sc.prepare()
    ensure_image.assert_called_once_with()
    ensure_network.assert_not_called()


def test_ensure_does_not_pull(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """ensure() no longer pulls — that moved to prepare()."""
    sc = _sidecar(tmp_path)
    ensure_image = mocker.patch.object(sc, "_ensure_image")
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_is_on_network", return_value=False)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-recovered")
    sc.ensure(use_host_net=False, agent_network=None)
    ensure_image.assert_not_called()


def test_ensure_health_fail_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """A failed health poll raises (the runner won't announce on a failed ensure)."""
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=False)
    mocker.patch.object(sc, "_load_secrets", return_value={"_secret_key": "k"})
    mocker.patch.object(sc, "_generate_master_key", return_value="sk-test-new")
    mocker.patch.object(sc, "_start")
    mocker.patch.object(sc, "_health_poll", return_value=False)
    mocker.patch(_DOCKER, return_value=("", 0))
    with pytest.raises(SystemExit):
        sc.ensure(use_host_net=False, agent_network=None)


def test_ensure_bridge_not_supported(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="bridge is not supported"):
        _sidecar(tmp_path).ensure(use_host_net=False, agent_network="bridge")


def test_ensure_sidecar_migration_restart(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Sidecar not on agent-wrap-net or host gets restarted."""
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc, "_ensure_network")
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_is_on_network", return_value=False)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    sc.ensure(use_host_net=False, agent_network=None)
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


# --- release (pure container stop; caller already decided + holds the lock) ---


def test_release_stops_and_unapproves(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    stopping = mocker.MagicMock()
    sc = _sidecar(tmp_path, on_stopping=stopping)
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    sc.release()
    stopping.assert_called_once_with("sk-test-key")
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


def test_release_no_stop_when_not_running(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Idempotent: a no-op when the container isn't running."""
    stopping = mocker.MagicMock()
    sc = _sidecar(tmp_path, on_stopping=stopping)
    mocker.patch.object(sc, "_is_running", return_value=False)
    mock_docker = mocker.patch(_DOCKER)
    sc.release()
    stopping.assert_not_called()
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert stop_calls == []


def test_release_non_tty_prints_plain_line(
    tmp_path: Path, mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture
) -> None:
    """Non-TTY stop prints a single plain status line and still stops the container."""
    sc = _sidecar(tmp_path)
    mocker.patch("sys.stderr.isatty", return_value=False)
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    sc.release()
    assert "litellm-sidecar: stopping…" in capsys.readouterr().err
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


def test_release_tty_finalizes(
    tmp_path: Path, mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture
) -> None:
    """TTY stop animates then clears the line and prints the 'stopped' finalize line."""
    sc = _sidecar(tmp_path)
    mocker.patch("sys.stderr.isatty", return_value=True)
    mocker.patch.object(sc, "_is_running", return_value=True)
    mocker.patch.object(sc, "_recover_master_key", return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    sc.release()
    err = capsys.readouterr().err
    assert "litellm-sidecar: stopped" in err
    assert "\033[2K" in err
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


# --- _health_poll ---


def _fake_monotonic(mocker: pytest_mock.MockFixture) -> None:
    call_count = [0]

    def fake() -> float:
        call_count[0] += 1
        return 0.0 if call_count[0] == 1 else 0.5

    mocker.patch("time.monotonic", side_effect=fake)
    mocker.patch("sys.stderr.isatty", return_value=False)


def test_health_poll_healthy_quick(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_DOCKER, return_value=("healthy", 0))
    mocker.patch.object(sc, "_is_running", return_value=True)
    _fake_monotonic(mocker)
    assert sc._health_poll() is True


def test_health_poll_unhealthy(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_DOCKER, return_value=("unhealthy", 0))
    mocker.patch.object(sc, "_is_running", return_value=True)
    _fake_monotonic(mocker)
    assert sc._health_poll() is False


def test_health_poll_container_gone(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_DOCKER, return_value=("", 1))
    _fake_monotonic(mocker)
    assert sc._health_poll() is False


# --- _ensure_image ---


def test_ensure_image_present_skips_pull(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_IMAGE_EXISTS, return_value=True)
    mock_docker = mocker.patch(_DOCKER)
    sc._ensure_image()
    pull_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "pull"]
    assert pull_calls == []


def test_ensure_image_absent_pulls(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_IMAGE_EXISTS, return_value=False)
    mock_docker = mocker.patch(_DOCKER, return_value=("", 0))
    sc._ensure_image()
    pull_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "pull"]
    assert len(pull_calls) == 1


def test_ensure_image_pull_fails_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_IMAGE_EXISTS, return_value=False)
    mocker.patch(_DOCKER, return_value=("", 1))
    with pytest.raises(SystemExit, match="failed to pull"):
        sc._ensure_image()


# --- _start ---


def test_start_creates_container(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    (tmp_path / "config.yaml").write_text("model: test")
    sc = _sidecar(tmp_path)
    mock_docker = mocker.patch(_DOCKER, side_effect=[("", 1), ("", 0)])
    sc._start("upstream-key", "sk-test-master", "bridge")
    assert any("run" in str(c) for c in mock_docker.call_args_list)


def test_start_reaps_stopped_container(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    (tmp_path / "config.yaml").write_text("model: test")
    sc = _sidecar(tmp_path)
    mock_docker = mocker.patch(_DOCKER, side_effect=[("", 0), ("", 0), ("", 0)])
    sc._start("upstream-key", "sk-test-master", "bridge")
    calls = [c.args[0] for c in mock_docker.call_args_list if c.args]
    assert "rm" in calls


def test_start_mounts_callback_and_log_dir(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """_start mounts the logging callback and the host log dir into the sidecar."""
    (tmp_path / "config.yaml").write_text("model: test")
    log_dir = tmp_path / "logs"
    sc = _sidecar(tmp_path, log_dir=log_dir)
    mock_docker = mocker.patch(_DOCKER, side_effect=[("", 1), ("", 0)])
    sc._start("upstream-key", "sk-test-master", "bridge")

    run_call = next(c for c in mock_docker.call_args_list if c.args and c.args[0] == "run")
    run_args = list(run_call.args)
    assert any(a.endswith("/etc/litellm/callback.py:ro") for a in run_args)
    assert f"{log_dir}:/var/log/agent-wrap" in run_args
    # The provider name is passed to the sidecar so the callback can route logs.
    assert "AGENT_WRAP_PROVIDER=litellm-test" in run_args
    # The host log dir is created so the bind mount has a source.
    assert log_dir.is_dir()


def test_start_missing_config_raises(tmp_path: Path) -> None:
    # No config.yaml written → _config_path raises.
    with pytest.raises(SystemExit, match="config not found"):
        _sidecar(tmp_path)._start("upstream-key", "sk-test-master", "bridge")


# --- _recover_master_key ---


def test_recover_master_key_success(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        _DOCKER, return_value=("ENV1=val\nLITELLM_MASTER_KEY=sk-test-recovered\nENV2=val2", 0)
    )
    assert _sidecar(tmp_path)._recover_master_key() == "sk-test-recovered"


def test_recover_master_key_absent_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, return_value=("ENV1=val\nENV2=val2", 0))
    with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY not recoverable"):
        _sidecar(tmp_path)._recover_master_key()


def test_recover_master_key_container_gone_raises(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_DOCKER, return_value=("", 1))
    with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY not recoverable"):
        _sidecar(tmp_path)._recover_master_key()
