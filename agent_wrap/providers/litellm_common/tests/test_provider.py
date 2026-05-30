# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/provider.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.providers.litellm_common.provider import LiteLLMProvider, _docker


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


# --- _docker helper ---


def test_docker_returns_tuple(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = "hello"
    mock_run.return_value.returncode = 0
    stdout, rc = _docker("info")
    assert stdout == "hello"
    assert rc == 0


def test_docker_timeout(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    import subprocess

    mock_run.side_effect = subprocess.TimeoutExpired(cmd="docker", timeout=30)
    stdout, rc = _docker("info")
    assert stdout == ""
    assert rc == 1


def test_docker_file_not_found(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.side_effect = FileNotFoundError()
    stdout, rc = _docker("info")
    assert stdout == ""
    assert rc == 1


def test_docker_check_true_raises(mocker: pytest_mock.MockFixture) -> None:
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.stdout = ""
    mock_run.return_value.returncode = 1
    mock_run.return_value.stderr = "error"
    with pytest.raises(RuntimeError, match="docker info failed"):
        _docker("info", check=True)


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
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("true", 0)
    assert p._is_running() is True


def test_is_running_false(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("false", 0)
    assert p._is_running() is False


def test_is_running_error(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("", 1)
    assert p._is_running() is False


def test_is_on_network_true(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("agent-wrap-net\nhost\n", 0)
    assert p._is_on_network("host") is True


def test_is_on_network_false(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("agent-wrap-net\n", 0)
    assert p._is_on_network("host") is False


def test_ensure_network_exists(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("", 0)
    p._ensure_network()
    calls = [c.args for c in mock_docker.call_args_list]
    assert any("network" in c and "inspect" in c for c in calls)


def test_ensure_network_creates(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.side_effect = [
        ("", 1),  # inspect fails
        ("", 0),  # create succeeds
    ]
    p._ensure_network()


def test_ensure_network_create_fails(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.side_effect = [
        ("", 1),  # inspect fails
        ("", 1),  # create fails
    ]
    with pytest.raises(SystemExit):
        p._ensure_network()


def test_attach_to_network_already_connected(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mocker.patch("agent_wrap.providers.litellm_common.provider._docker", return_value=("", 0))
    mocker.patch.object(p, "_is_on_network", return_value=True)
    p._attach_to_network("mynet")


def test_sidecar_ip_on_network(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("172.18.0.2", 0)
    ip = p._sidecar_ip_on_network("agent-wrap-net")
    assert ip == "172.18.0.2"


def test_sidecar_ip_on_network_failure(mocker: pytest_mock.MockFixture) -> None:
    p = ConcreteTestProvider()
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
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
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
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
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
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
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
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
    mock_docker = mocker.patch("agent_wrap.providers.litellm_common.provider._docker")
    mock_docker.return_value = ("", 0)
    p.release("test-1")
    stop_calls = [c for c in mock_docker.call_args_list if c.args and c.args[0] == "stop"]
    assert len(stop_calls) == 1
