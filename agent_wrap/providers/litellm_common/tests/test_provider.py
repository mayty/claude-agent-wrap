# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/provider.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.providers.litellm_common.provider import LiteLLMProvider


class ConcreteTestProvider(LiteLLMProvider):
    """Concrete subclass for testing abstract LiteLLMProvider."""

    image = "test-image:latest"
    lock_file = "lock"
    refcount_file = "refcount"
    master_key_prefix = "sk-test-"

    def __init__(self, state_dir: Path | None = None) -> None:
        super().__init__()
        self._test_state_dir = state_dir

    def _state_dir(self) -> Path:
        if self._test_state_dir:
            return self._test_state_dir
        return super()._state_dir()

    def get_sidecar_env(self, secrets: dict) -> dict[str, str]:
        return {"UPSTREAM_KEY": secrets.get("_secret_key", "")}

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {"API_KEY": master_key, "BASE_URL": base_url}

    def read_secret_key(self, secrets: dict) -> str:
        return secrets.get("_secret_key", "")

    def get_sidecar_cmd_args(self) -> list[str]:
        return []


# --- Pure/light methods ---


def test_generate_master_key() -> None:
    p = ConcreteTestProvider()
    key = p._generate_master_key()
    assert key.startswith("sk-test-")
    assert "-" not in key.removeprefix("sk-test-")


def test_refcount_path(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p._refcount_path() == tmp_path / "refcount"


def test_get_label_args() -> None:
    p = ConcreteTestProvider()
    result = p.get_label_args("my-instance-123")
    assert "--label" in result
    assert "agent-wrap.role=claude-agent" in result
    assert "agent-wrap.instance-id=my-instance-123" in result
    assert "--name" in result
    assert "claude-agent-my-instance-123" in result


def test_get_label_args_empty_instance() -> None:
    p = ConcreteTestProvider()
    assert p.get_label_args("") == []


def test_get_run_args_returns_copy() -> None:
    p = ConcreteTestProvider()
    p._run_args = ["-e", "FOO=bar"]
    result = p.get_run_args()
    assert result == ["-e", "FOO=bar"]
    result.append("-x")
    assert p._run_args == ["-e", "FOO=bar"]  # original unchanged


# --- File-based methods ---


def test_register_instance(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("inst-1")
    content = (tmp_path / "refcount").read_text()
    assert "inst-1" in content


def test_register_instance_no_dupes(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("inst-1")
    p._register_instance("inst-1")
    lines = (tmp_path / "refcount").read_text().splitlines()
    assert lines.count("inst-1") == 1


def test_unregister_instance(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("inst-1")
    p._register_instance("inst-2")
    p._unregister_instance("inst-1")
    content = (tmp_path / "refcount").read_text()
    assert "inst-1" not in content
    assert "inst-2" in content


def test_unregister_missing_file(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._unregister_instance("ghost")  # should not raise


def test_has_active_instances_true(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("inst-1")
    assert p._has_active_instances() is True


def test_has_active_instances_false(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p._has_active_instances() is False


def test_has_active_instances_empty_file(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    (tmp_path / "refcount").write_text("")
    assert p._has_active_instances() is False


# --- Docker-dependent methods ---


def test_is_running_true(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("true", 0)
    assert p._is_running() is True


def test_is_running_false(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("false", 0)
    assert p._is_running() is False


def test_is_running_error(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 1)
    assert p._is_running() is False


def test_is_on_network_true(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("agent-wrap-net\nhost\n", 0)
    assert p._is_on_network("host") is True


def test_is_on_network_false(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("agent-wrap-net\n", 0)
    assert p._is_on_network("host") is False


def test_ensure_network_exists(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 0)
    p._ensure_network()
    calls = [c.args for c in mock_docker.call_args_list]
    assert any("network" in c and "inspect" in c for c in calls)


def test_ensure_network_creates(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.side_effect = [
        ("", 1),  # inspect fails
        ("", 0),  # create succeeds
    ]
    p._ensure_network()


def test_ensure_network_create_fails(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.side_effect = [
        ("", 1),  # inspect fails
        ("", 1),  # create fails
    ]
    with pytest.raises(SystemExit):
        p._ensure_network()


def test_attach_to_network_already_connected(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run", return_value=("", 0))
    mocker.patch.object(p, "_is_on_network", return_value=True)
    p._attach_to_network("mynet")


def test_sidecar_ip_on_network(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("172.18.0.2", 0)
    ip = p._sidecar_ip_on_network("agent-wrap-net")
    assert ip == "172.18.0.2"


def test_sidecar_ip_on_network_failure(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 1)
    ip = p._sidecar_ip_on_network("agent-wrap-net")
    assert ip == ""


# --- _build_connectivity_args ---


def test_connectivity_host_sidecar_host_agent() -> None:
    p = ConcreteTestProvider()
    p._master_key = "sk-test-abc"
    result = p._build_connectivity_args("host", agent_in_host_netns=True, agent_network=None)
    assert "--add-host" in result
    assert "agent-wrap-litellm:127.0.0.1" in result


def test_connectivity_host_sidecar_bridge_agent() -> None:
    p = ConcreteTestProvider()
    p._master_key = "sk-test-abc"
    result = p._build_connectivity_args("host", agent_in_host_netns=False, agent_network=None)
    assert "agent-wrap-litellm:host-gateway" in result


def test_connectivity_bridge_sidecar_host_netns_agent(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    p._master_key = "sk-test-abc"
    mocker.patch.object(p, "_sidecar_ip_on_network", return_value="172.18.0.2")
    result = p._build_connectivity_args("bridge", agent_in_host_netns=True, agent_network=None)
    assert "agent-wrap-litellm:172.18.0.2" in result


def test_connectivity_bridge_sidecar_no_agent_network() -> None:
    p = ConcreteTestProvider()
    p._master_key = "sk-test-abc"
    result = p._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network=None)
    assert "--network" in result
    assert "agent-wrap-net" in result


def test_connectivity_bridge_sidecar_custom_agent_network() -> None:
    p = ConcreteTestProvider()
    p._master_key = "sk-test-abc"
    result = p._build_connectivity_args("bridge", agent_in_host_netns=False, agent_network="mynet")
    assert "--network" not in result
    assert "-e" in result


# --- _health_end ---


def test_health_end_tty_success(capsys: pytest.CaptureFixture) -> None:
    LiteLLMProvider._health_end(is_tty=True, success=True, elapsed=5.3)
    output = capsys.readouterr().err
    assert "ready" in output


def test_health_end_tty_failure(capsys: pytest.CaptureFixture) -> None:
    LiteLLMProvider._health_end(is_tty=True, success=False, elapsed=90.0)
    output = capsys.readouterr().err
    assert output == "\n"


def test_health_end_non_tty_no_output(capsys: pytest.CaptureFixture) -> None:
    LiteLLMProvider._health_end(is_tty=False, success=True, elapsed=5.0)
    output = capsys.readouterr().err
    assert output == ""


# --- Refcount flow ---


def test_refcount_full_cycle(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("test-1")
    assert p._has_active_instances() is True
    p._unregister_instance("test-1")
    assert p._has_active_instances() is False


def test_reconcile_drops_stale(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("stale-1")
    p._register_instance("live-1")
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("live-1\n", 0)
    p._reconcile_refcount()
    content = (tmp_path / "refcount").read_text()
    assert "stale-1" not in content
    assert "live-1" in content


def test_reconcile_empty_refcount_file(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    (tmp_path / "refcount").write_text("")
    p._reconcile_refcount()  # should not raise


def test_reconcile_docker_fails_keeps_entries(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("keep-1")
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 1)
    p._reconcile_refcount()
    content = (tmp_path / "refcount").read_text()
    assert "keep-1" in content


# --- ensure (happy path, mocked) ---


def test_ensure_existing_sidecar(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch.object(p, "_acquire_lock")
    mocker.patch.object(p, "_ensure_network")
    mocker.patch.object(p, "_is_running", return_value=True)
    mocker.patch.object(p, "_is_on_network", return_value=False)
    mocker.patch.object(p, "_recover_master_key", return_value="sk-test-recovered")
    mocker.patch.object(p, "_register_instance")
    mocker.patch("agent_wrap.providers.litellm_common.provider.fcntl")
    p.ensure(use_host_net=False, instance_id="test-1", agent_network=None)
    assert p._master_key == "sk-test-recovered"


def test_ensure_first_launch(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch.object(p, "_acquire_lock")
    mocker.patch.object(p, "_ensure_network")
    mocker.patch.object(p, "_is_running", return_value=False)
    mocker.patch.object(p, "_load_secrets", return_value={"_secret_key": "aws-key"})
    mocker.patch.object(p, "_generate_master_key", return_value="sk-test-new")
    mocker.patch.object(p, "_ensure_image")
    mocker.patch.object(p, "_start")
    mocker.patch.object(p, "_health_poll", return_value=True)
    mocker.patch.object(p, "_register_instance")
    mocker.patch("agent_wrap.providers.litellm_common.provider.fcntl")
    p.ensure(use_host_net=False, instance_id="test-1", agent_network=None)
    assert p._master_key == "sk-test-new"


def test_ensure_bridge_not_supported(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch.object(p, "_acquire_lock")
    mocker.patch.object(p, "_ensure_network")
    mocker.patch("agent_wrap.providers.litellm_common.provider.fcntl")
    with pytest.raises(SystemExit, match="bridge is not supported"):
        p.ensure(use_host_net=False, instance_id="test-1", agent_network="bridge")


# --- release ---


def test_release_no_lock_file(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p.release("test-1")  # should not raise


def test_release_empty_instance_id() -> None:
    p = ConcreteTestProvider()
    p.release("")  # should return immediately


def test_release_with_active_instances(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch.object(p, "_state_dir").return_value = tmp_path
    mocker.patch.object(p, "_is_running", return_value=True)
    mocker.patch("fcntl.flock")
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("other-1\n", 0)
    p.release("test-1")
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 0


def test_release_last_instance_stops_sidecar(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    p._register_instance("test-1")
    (tmp_path / "lock").touch()
    mocker.patch.object(p, "_is_running", return_value=True)
    mocker.patch("fcntl.flock")
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 0)
    p.release("test-1")
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1


# --- _health_poll ---


def test_health_poll_healthy_quick(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("healthy", 0)
    mocker.patch.object(p, "_is_running", return_value=True)
    call_count = [0]

    def fake_monotonic():
        call_count[0] += 1
        return 0.0 if call_count[0] == 1 else 0.5  # start, then after check

    mocker.patch("time.monotonic", side_effect=fake_monotonic)
    mocker.patch("sys.stderr.isatty", return_value=False)
    assert p._health_poll() is True


def test_health_poll_unhealthy(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("unhealthy", 0)
    mocker.patch.object(p, "_is_running", return_value=True)
    call_count = [0]

    def fake_monotonic():
        call_count[0] += 1
        return 0.0 if call_count[0] == 1 else 0.5

    mocker.patch("time.monotonic", side_effect=fake_monotonic)
    mocker.patch("sys.stderr.isatty", return_value=False)
    assert p._health_poll() is False


def test_health_poll_container_gone(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 1)  # inspect fails
    call_count = [0]

    def fake_monotonic():
        call_count[0] += 1
        return 0.0 if call_count[0] == 1 else 0.5

    mocker.patch("time.monotonic", side_effect=fake_monotonic)
    mocker.patch("sys.stderr.isatty", return_value=False)
    assert p._health_poll() is False


# --- _ensure_image ---


def test_ensure_image_present_skips_pull(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch("agent_wrap.providers.litellm_common.provider.image_exists", return_value=True)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    p._ensure_image()
    pull_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "pull"]
    assert pull_calls == []


def test_ensure_image_absent_pulls(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch("agent_wrap.providers.litellm_common.provider.image_exists", return_value=False)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 0)
    p._ensure_image()
    pull_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "pull"]
    assert len(pull_calls) == 1


def test_ensure_image_pull_fails_raises(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch("agent_wrap.providers.litellm_common.provider.image_exists", return_value=False)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 1)
    with pytest.raises(SystemExit, match="failed to pull"):
        p._ensure_image()


# --- _start ---


def test_start_creates_container(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    # Create a fake config.yaml
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("model: test")
    mocker.patch.object(p, "_config_path", return_value=config_dir / "config.yaml")

    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.side_effect = [
        ("", 1),  # container inspect fails (no existing container)
        ("", 0),  # docker run succeeds
    ]
    p._start("upstream-key", "sk-test-master", "bridge")
    # Verify docker run was called with --name
    calls = [str(c) for c in mock_docker.call_args_list]
    assert any("run" in c for c in calls)


def test_start_reaps_stopped_container(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("model: test")
    mocker.patch.object(p, "_config_path", return_value=config_dir / "config.yaml")

    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.side_effect = [
        ("", 0),  # container inspect succeeds (existing stopped container)
        ("", 0),  # docker rm -f
        ("", 0),  # docker run
    ]
    p._start("upstream-key", "sk-test-master", "bridge")
    # Should have called rm -f before run
    calls = [c.args[0] for c in mock_docker.call_args_list if c.args]
    assert "rm" in calls


# --- _recover_master_key ---


def test_recover_master_key_success(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("ENV1=val\nLITELLM_MASTER_KEY=sk-test-recovered\nENV2=val2", 0)
    key = p._recover_master_key()
    assert key == "sk-test-recovered"


def test_recover_master_key_absent_raises(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("ENV1=val\nENV2=val2", 0)
    with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY not recoverable"):
        p._recover_master_key()


def test_recover_master_key_container_gone_raises(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 1)
    with pytest.raises(SystemExit, match="LITELLM_MASTER_KEY not recoverable"):
        p._recover_master_key()


# --- _acquire_lock ---


def test_acquire_lock_succeeds(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    mock_fcntl = mocker.patch("fcntl.flock")
    p._acquire_lock()
    assert mock_fcntl.called
    assert p._lock_file is not None
    p._lock_file.close()


# --- _ensure_sidecar migration ---


def test_ensure_sidecar_migration_restart(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Sidecar not on agent-wrap-net or host gets restarted."""
    p = ConcreteTestProvider(state_dir=tmp_path)
    mocker.patch.object(p, "_acquire_lock")
    mocker.patch.object(p, "_ensure_network")
    mocker.patch.object(p, "_is_running", return_value=True)
    mocker.patch.object(p, "_is_on_network", return_value=False)  # not on either network
    mocker.patch.object(p, "_recover_master_key", return_value="sk-test-key")
    mocker.patch.object(p, "_register_instance")
    mocker.patch("agent_wrap.providers.litellm_common.provider.fcntl")
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider.docker_run")
    mock_docker.return_value = ("", 0)

    p.ensure(use_host_net=False, instance_id="test-1", agent_network=None)
    # Should have stopped the old sidecar
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1
