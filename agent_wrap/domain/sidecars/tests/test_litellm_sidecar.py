# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/domain/sidecars/litellm.py."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest

from agent_wrap.constants import LITELLM_SIDECAR_LABEL, PollResult
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.sidecars.litellm import LiteLLMSidecar
from agent_wrap.domain.sidecars.models import LiteLLMSidecarConfig
from agent_wrap.lib.path_hash import project_path_hash

if TYPE_CHECKING:
    import pytest_mock


def _config(tmp_path: Path, **overrides: object) -> LiteLLMSidecarConfig:
    """Build a LiteLLMSidecarConfig with simple hooks, rooted at *tmp_path*."""
    defaults: dict[str, Any] = {
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
        "get_sidecar_env": lambda secrets: {"UPSTREAM_KEY": secrets.get("api_key", "")},
        "get_agent_env": lambda master_key, base_url: {"API_KEY": master_key, "BASE_URL": base_url},
        "on_started": lambda _key: None,
        "on_stopping": lambda _key: None,
        "required_secrets": [],
    }
    defaults.update(overrides)
    return LiteLLMSidecarConfig(**defaults)  # type: ignore[arg-type]


def _sidecar(tmp_path: Path, **overrides: object) -> LiteLLMSidecar:
    return LiteLLMSidecar(_config(tmp_path, **overrides), display_service=Mock(spec=DisplayService))


_DOCKER = "agent_wrap.domain.sidecars.litellm.docker_run"
_IMAGE_EXISTS = "agent_wrap.domain.sidecars.litellm.image_exists"


def test_timing(tmp_path: Path) -> None:
    sc = _sidecar(tmp_path)
    assert sc.cold_start_time == 300.0
    assert sc.short_circuit_time == 30.0


def test_generate_master_key(tmp_path: Path) -> None:
    key = _sidecar(tmp_path)._generate_master_key()
    assert key.startswith("sk-test-")
    assert "-" not in key.removeprefix("sk-test-")


def test_is_running_true(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("true", 0))
    assert _sidecar(tmp_path)._is_running() is True


def test_is_running_false(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("false", 0))
    assert _sidecar(tmp_path)._is_running() is False


def test_is_running_error(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    assert _sidecar(tmp_path)._is_running() is False


def test_is_on_network_true(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("agent-wrap-net\nhost\n", 0))
    assert _sidecar(tmp_path)._is_on_network("host") is True


def test_is_on_network_false(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("agent-wrap-net\n", 0))
    assert _sidecar(tmp_path)._is_on_network("host") is False


def test_ensure_network_exists(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    _sidecar(tmp_path)._ensure_network()
    calls = [c.args for c in mock_docker.call_args_list]
    assert any("network" in c and "inspect" in c for c in calls)


def test_ensure_network_creates(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, side_effect=[("", 1), ("", 0)])
    _sidecar(tmp_path)._ensure_network()


def test_ensure_network_create_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, side_effect=[("", 1), ("", 1)])
    with pytest.raises(SystemExit):
        _sidecar(tmp_path)._ensure_network()


def test_attach_to_network_already_connected(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    mocker.patch.object(sc, "_is_on_network", autospec=True, return_value=True)
    sc._attach_to_network("mynet")


def test_sidecar_ip_on_network(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("172.18.0.2", 0))
    assert _sidecar(tmp_path)._sidecar_ip_on_network("agent-wrap-net") == "172.18.0.2"


def test_sidecar_ip_on_network_failure(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    assert _sidecar(tmp_path)._sidecar_ip_on_network("agent-wrap-net") == ""


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


def test_connectivity_injects_log_prefix_header(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    sc = _sidecar(tmp_path)
    sc._master_key = "sk-test-abc"
    cwd = Path("/some/project")
    mocker.patch("agent_wrap.domain.sidecars.litellm.Path.cwd", return_value=cwd)
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
    mocker.patch("agent_wrap.domain.sidecars.litellm.Path.cwd", return_value=cwd)
    result = sc._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network="mynet")
    value = f"x-foo: bar\nx-agent-wrap-log-prefix: {project_path_hash(cwd)}"
    assert f"ANTHROPIC_CUSTOM_HEADERS={value}" in result


def test_ensure_returns_connectivity_args(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc, "_ensure_image", autospec=True)
    mocker.patch.object(sc, "_ensure_network", autospec=True)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_is_on_network", autospec=True, return_value=False)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-recovered")
    result = sc.ensure(use_host_net=False, agent_network=None)
    # Returns docker run flags (env + connectivity), including the log header.
    assert "-e" in result
    assert any("ANTHROPIC_CUSTOM_HEADERS=" in a for a in result)


def test_ensure_existing_sidecar_does_not_reapprove(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    started_calls: list[str] = []
    sc = _sidecar(tmp_path, on_started=started_calls.append)
    mocker.patch.object(sc, "_ensure_network", autospec=True)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_is_on_network", autospec=True, return_value=False)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-recovered")
    sc.ensure(use_host_net=False, agent_network=None)
    assert sc._master_key == "sk-test-recovered"
    assert started_calls == []


def test_ensure_first_launch_approves_once(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    started_calls: list[str] = []
    sc = _sidecar(tmp_path, on_started=started_calls.append)
    mocker.patch.object(sc, "_ensure_network", autospec=True)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=False)
    mocker.patch.object(sc, "_generate_master_key", autospec=True, return_value="sk-test-new")
    mocker.patch.object(sc, "_start", autospec=True)
    mocker.patch.object(sc, "_health_poll", autospec=True, return_value=True)
    sc.ensure(use_host_net=False, agent_network=None, secrets={"api_key": "k"})
    assert sc._master_key == "sk-test-new"
    assert started_calls == ["sk-test-new"]


def test_prepare_pulls_image(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """prepare() pulls the image (lock-free pre-work) and does nothing else."""
    sc = _sidecar(tmp_path)
    ensure_image = mocker.patch.object(sc, "_ensure_image", autospec=True)
    ensure_network = mocker.patch.object(sc, "_ensure_network", autospec=True)
    sc.prepare()
    ensure_image.assert_called_once_with()
    ensure_network.assert_not_called()


def test_ensure_does_not_pull(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """ensure() no longer pulls — that moved to prepare()."""
    sc = _sidecar(tmp_path)
    ensure_image = mocker.patch.object(sc, "_ensure_image", autospec=True)
    mocker.patch.object(sc, "_ensure_network", autospec=True)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_is_on_network", autospec=True, return_value=False)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-recovered")
    sc.ensure(use_host_net=False, agent_network=None)
    ensure_image.assert_not_called()


def test_ensure_health_fail_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """A failed health poll raises (the runner won't announce on a failed ensure)."""
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc, "_ensure_network", autospec=True)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=False)
    mocker.patch.object(sc, "_generate_master_key", autospec=True, return_value="sk-test-new")
    mocker.patch.object(sc, "_start", autospec=True)
    mocker.patch.object(sc, "_health_poll", autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    with pytest.raises(SystemExit):
        sc.ensure(use_host_net=False, agent_network=None, secrets={"api_key": "k"})

    # Logs must stream straight through (capture=False) so a startup traceback
    # on the container's stderr reaches the user instead of being swallowed.
    logs_calls = [c for c in mock_docker.call_args_list if "logs" in c.args[:1]]
    assert len(logs_calls) == 1
    assert logs_calls[0].kwargs.get("capture") is False


def test_ensure_bridge_not_supported(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="bridge is not supported"):
        _sidecar(tmp_path).ensure(use_host_net=False, agent_network="bridge")


def test_ensure_sidecar_migration_restart(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Sidecar not on agent-wrap-net or host gets restarted."""
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc, "_ensure_network", autospec=True)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_is_on_network", autospec=True, return_value=False)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sc.ensure(use_host_net=False, agent_network=None)
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


def test_release_stops_and_unapproves(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    stopping_calls: list[str] = []
    sc = _sidecar(tmp_path, on_stopping=stopping_calls.append)
    mocker.patch.object(sc._display, "spin_while", side_effect=lambda **kw: kw["work"]())
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sc.release()
    assert stopping_calls == ["sk-test-key"]
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


def test_release_no_stop_when_not_running(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Idempotent: a no-op when the container isn't running."""
    stopping_calls: list[str] = []
    sc = _sidecar(tmp_path, on_stopping=stopping_calls.append)
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True)
    sc.release()
    assert stopping_calls == []
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert stop_calls == []


def test_release_non_tty_prints_plain_line(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Non-TTY stop prints a single plain status line and still stops the container."""
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc._display, "spin_while", side_effect=lambda **kw: kw["work"]())
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sc.release()
    sc._display.spin_while.assert_called_once_with(  # type: ignore[union-attr]
        label=LITELLM_SIDECAR_LABEL,
        message="stopping…",
        done_message="stopped",
        work=mocker.ANY,
    )
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


def test_release_tty_finalizes(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """TTY stop animates then clears the line and prints the 'stopped' finalize line."""
    sc = _sidecar(tmp_path)
    mocker.patch.object(sc._display, "spin_while", side_effect=lambda **kw: kw["work"]())
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    mocker.patch.object(sc, "_recover_master_key", autospec=True, return_value="sk-test-key")
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sc.release()
    sc._display.spin_while.assert_called_once_with(  # type: ignore[union-attr]
        label=LITELLM_SIDECAR_LABEL,
        message="stopping…",
        done_message="stopped",
        work=mocker.ANY,
    )
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


@pytest.fixture
def fake_monotonic(mocker: pytest_mock.MockFixture) -> None:
    """Patch time.monotonic so it returns 0.0 then 0.5."""
    call_count = [0]

    def fake() -> float:
        call_count[0] += 1
        return 0.0 if call_count[0] == 1 else 0.5

    mocker.patch("time.monotonic", side_effect=fake)
    mocker.patch("sys.stderr.isatty", return_value=False)


def test_health_poll_healthy_quick(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch.object(
        sc._display, "poll_until", side_effect=lambda **kw: kw["poll"]()[0] == PollResult.SUCCESS
    )
    mocker.patch(_DOCKER, autospec=True, return_value=("healthy", 0))
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    assert sc._health_poll() is True


def test_health_poll_unhealthy(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch.object(
        sc._display, "poll_until", side_effect=lambda **kw: kw["poll"]()[0] == PollResult.SUCCESS
    )
    mocker.patch(_DOCKER, autospec=True, return_value=("unhealthy", 0))
    mocker.patch.object(sc, "_is_running", autospec=True, return_value=True)
    assert sc._health_poll() is False


def test_health_poll_container_gone(
    tmp_path: Path,
    mocker: pytest_mock.MockFixture,
) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch.object(
        sc._display, "poll_until", side_effect=lambda **kw: kw["poll"]()[0] == PollResult.SUCCESS
    )
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    assert sc._health_poll() is False


def test_ensure_image_present_skips_pull(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=True)
    mock_docker = mocker.patch(_DOCKER, autospec=True)
    sc._ensure_image()
    pull_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "pull"]
    assert pull_calls == []


def test_ensure_image_absent_pulls(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=False)
    mock_docker = mocker.patch(_DOCKER, autospec=True, return_value=("", 0))
    sc._ensure_image()
    pull_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "pull"]
    assert len(pull_calls) == 1


def test_ensure_image_pull_fails_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    sc = _sidecar(tmp_path)
    mocker.patch(_IMAGE_EXISTS, autospec=True, return_value=False)
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    with pytest.raises(SystemExit, match="failed to pull"):
        sc._ensure_image()


def test_start_creates_container(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    (tmp_path / "config.yaml").write_text("model: test")
    sc = _sidecar(tmp_path)
    mock_docker = mocker.patch(_DOCKER, autospec=True, side_effect=[("", 1), ("", 0)])
    sc._start({"api_key": "upstream-key"}, "sk-test-master", "bridge")
    assert any("run" in str(c) for c in mock_docker.call_args_list)


def test_start_reaps_stopped_container(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    (tmp_path / "config.yaml").write_text("model: test")
    sc = _sidecar(tmp_path)
    mock_docker = mocker.patch(_DOCKER, autospec=True, side_effect=[("", 0), ("", 0), ("", 0)])
    sc._start({"api_key": "upstream-key"}, "sk-test-master", "bridge")
    calls = [c.args[0] for c in mock_docker.call_args_list if c.args]
    assert "rm" in calls


def test_start_mounts_callback_and_log_dir(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """_start mounts the logging callback and the host log dir into the sidecar."""
    (tmp_path / "config.yaml").write_text("model: test")
    log_dir = tmp_path / "logs"
    callback_dir = tmp_path / "callbacks"
    callback_dir.mkdir()
    (callback_dir / "callback.py").touch()
    sc = _sidecar(tmp_path, log_dir=log_dir, callback_dir=callback_dir)
    mock_docker = mocker.patch(_DOCKER, autospec=True, side_effect=[("", 1), ("", 0)])
    sc._start({"api_key": "upstream-key"}, "sk-test-master", "bridge")

    run_call = next(c for c in mock_docker.call_args_list if c.args and c.args[0] == "run")
    run_args = list(run_call.args)
    assert any(a.endswith("/etc/litellm/callback.py:ro") for a in run_args)
    assert f"{log_dir}:/var/log/agent-wrap" in run_args
    # The provider name is passed to the sidecar so the callback can route logs.
    assert "AGENT_WRAP_PROVIDER=litellm-test" in run_args
    # The host log dir is created so the bind mount has a source.
    assert log_dir.is_dir()


def test_start_passes_every_declared_secret_to_the_provider_hook(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    """The resolved secrets dict reaches get_sidecar_env keyed by its declared names."""
    (tmp_path / "config.yaml").write_text("model: test")
    sc = _sidecar(
        tmp_path,
        get_sidecar_env=lambda secrets: {
            "PRIMARY": secrets["primary_key"],
            "SECONDARY": secrets["secondary_key"],
        },
    )
    mock_docker = mocker.patch(_DOCKER, autospec=True, side_effect=[("", 1), ("", 0)])
    sc._start({"primary_key": "one", "secondary_key": "two"}, "sk-test-master", "bridge")

    run_args = list(next(c for c in mock_docker.call_args_list if c.args[0] == "run").args)
    assert "PRIMARY=one" in run_args
    assert "SECONDARY=two" in run_args


def test_start_with_no_secrets_declared(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """A provider fronting an unauthenticated upstream declares and receives nothing."""
    (tmp_path / "config.yaml").write_text("model: test")
    sc = _sidecar(tmp_path, get_sidecar_env=lambda _secrets: {}, required_secrets=[])
    mock_docker = mocker.patch(_DOCKER, autospec=True, side_effect=[("", 1), ("", 0)])
    sc._start({}, "sk-test-master", "bridge")

    run_args = list(next(c for c in mock_docker.call_args_list if c.args[0] == "run").args)
    # The master key is still minted and injected; only the upstream token is absent.
    assert "LITELLM_MASTER_KEY=sk-test-master" in run_args


def test_start_missing_config_raises(tmp_path: Path) -> None:
    # No config.yaml written → _config_path raises.
    with pytest.raises(SystemExit, match="config not found"):
        _sidecar(tmp_path)._start({"api_key": "upstream-key"}, "sk-test-master", "bridge")


def test_recover_master_key_success(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(
        _DOCKER, return_value=("ENV1=val\nLITELLM_MASTER_KEY=sk-test-recovered\nENV2=val2", 0)
    )
    assert _sidecar(tmp_path)._recover_master_key() == "sk-test-recovered"


def test_recover_master_key_absent_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("ENV1=val\nENV2=val2", 0))
    with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY not recoverable"):
        _sidecar(tmp_path)._recover_master_key()


def test_recover_master_key_container_gone_raises(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(_DOCKER, autospec=True, return_value=("", 1))
    with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY not recoverable"):
        _sidecar(tmp_path)._recover_master_key()
