# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.launch.launch.LaunchService."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.config.service import ConfigService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.launch.service import LaunchService
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.secrets.service import SecretsService
from agent_wrap.domain.sidecars.base import Sidecar
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.domain.updates.service import UpdateService
from agent_wrap.exceptions import SecretNotFoundError

if TYPE_CHECKING:
    import pytest_mock


@pytest.fixture
def launch_svc(mocker: pytest_mock.MockFixture) -> LaunchService:
    """Return a LaunchService with spec-mocked dependencies."""
    real_build = BuildService(
        update_service=mocker.Mock(spec=UpdateService),
        display_service=mocker.Mock(spec=DisplayService),
    )
    build_svc = mocker.Mock(spec=BuildService, wraps=real_build)
    sidecar_svc = mocker.Mock(spec=SidecarService)
    sidecar_svc.role_label = SidecarService.role_label
    sidecar_svc.role_value = SidecarService.role_value
    return LaunchService(
        config_service=mocker.Mock(spec=ConfigService),
        secrets_service=mocker.Mock(spec=SecretsService),
        update_service=mocker.Mock(spec=UpdateService),
        provider_service=mocker.Mock(spec=ProviderService),
        sidecar_service=sidecar_svc,
        build_service=build_svc,
        display_service=mocker.Mock(spec=DisplayService),
    )


# --- resolve_agent_name ---


def test_resolve_agent_name_use_base(tmp_path: Path, launch_svc: LaunchService) -> None:
    result = launch_svc._resolve_agent_name(use_base=True, cwd=tmp_path)
    assert result == tmp_path.name.lower()


def test_resolve_agent_name_no_dockerfile(tmp_path: Path, launch_svc: LaunchService) -> None:
    result = launch_svc._resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == tmp_path.name.lower()


def test_resolve_agent_name_from_dockerfile(tmp_path: Path, launch_svc: LaunchService) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: my-custom-agent\nFROM claude-agent\n")
    result = launch_svc._resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == "my-custom-agent"


def test_resolve_agent_name_dockerfile_no_agent_name(
    tmp_path: Path, launch_svc: LaunchService
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("FROM claude-agent\n")
    result = launch_svc._resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == tmp_path.name.lower()


def test_resolve_agent_name_empty_sanitized(tmp_path: Path, launch_svc: LaunchService) -> None:
    bad_dir = tmp_path / "---"
    bad_dir.mkdir()
    result = launch_svc._resolve_agent_name(use_base=True, cwd=bad_dir)
    assert result == "agent"


def test_resolve_agent_name_empty_value_after_colon(
    tmp_path: Path, launch_svc: LaunchService
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: \nFROM claude-agent\n")
    result = launch_svc._resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == tmp_path.name.lower()


# --- resolve_sidecar_secrets ---


def test_resolve_secrets_optional_missing_skips(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    launch_svc._secrets = mocker.Mock(spec=SecretsService)
    launch_svc._secrets.read.side_effect = SecretNotFoundError("test", "desc")

    result = launch_svc._resolve_sidecar_secrets(
        "test", [("Key1", "desc")], optional=True, headless=False
    )
    assert result is None


def test_resolve_secrets_required_missing_non_tty(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    launch_svc._secrets = mocker.Mock(spec=SecretsService)
    launch_svc._secrets.read.side_effect = SecretNotFoundError("test", "desc")
    mocker.patch("sys.stdin.isatty", return_value=False)

    with pytest.raises(SystemExit):
        launch_svc._resolve_sidecar_secrets(
            "test", [("Key1", "desc")], optional=False, headless=False
        )


def test_resolve_secrets_required_missing_tty_prompts(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    launch_svc._secrets = mocker.Mock(spec=SecretsService)
    launch_svc._secrets.read.return_value = "entered-value"
    mocker.patch("sys.stdin.isatty", return_value=True)

    result = launch_svc._resolve_sidecar_secrets(
        "test", [("Key1", "desc")], optional=False, headless=False
    )
    assert result == {"Key1": "entered-value"}
    launch_svc._secrets.read.assert_called_once_with("test:Key1", "desc", prompt_on_missing=True)


def test_resolve_secrets_found_no_prompt(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    launch_svc._secrets = mocker.Mock(spec=SecretsService)
    launch_svc._secrets.read.return_value = "stored-value"

    result = launch_svc._resolve_sidecar_secrets(
        "test", [("Key1", "desc")], optional=False, headless=False
    )
    assert result == {"Key1": "stored-value"}
    launch_svc._secrets.read.assert_called_once_with("test:Key1", "desc", prompt_on_missing=False)


# --- is_headless ---


@pytest.mark.parametrize(
    ("claude_args", "expected"),
    [
        (["-p", "do a thing"], True),
        (["--print"], True),
        (["--bare"], True),
        (["--safe-mode"], True),
        (["--model", "x"], False),
        ([], False),
    ],
)
def test_is_headless(
    claude_args: list[str],
    expected: bool,  # noqa: FBT001
    launch_svc: LaunchService,
) -> None:
    assert launch_svc._is_headless(claude_args) is expected


# --- build_wslg_args ---


def test_build_wslg_args_not_present(
    tmp_path: Path, mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    fake_mnt = tmp_path / "mnt" / "wslg"
    mocker.patch(
        "agent_wrap.domain.launch.service.Path",
        lambda path: fake_mnt if str(path) == "/mnt/wslg" else Path(path),
    )
    result = launch_svc._build_wslg_args()
    assert result == []


def test_build_wslg_args_present(
    tmp_path: Path, mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    fake_mnt = tmp_path / "mnt" / "wslg"
    fake_mnt.mkdir(parents=True)
    mocker.patch(
        "agent_wrap.domain.launch.service.Path",
        lambda path: fake_mnt if str(path) == "/mnt/wslg" else Path(path),
    )
    result = launch_svc._build_wslg_args()
    assert "-v" in result
    assert "/mnt/wslg/runtime-dir:/mnt/wslg/runtime-dir" in result
    assert "/mnt/wslg/.X11-unix:/tmp/.X11-unix" in result
    assert "/mnt/wslg:/mnt/wslg" not in result
    assert f"{tmp_path}/ops/wl-paste-shim:/usr/local/bin/wl-paste:ro" in result
    assert "-e" in result
    assert "DISPLAY" in result
    assert "WAYLAND_DISPLAY" in result
    assert "XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir" in result


# --- build_env_args ---


def test_build_env_args_basic(launch_svc: LaunchService) -> None:
    result = launch_svc._build_env_args("myagent", "myagent-uuid", "/home/ubuntu")
    assert "-e" in result
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in result
    assert "AGENT_NAME=myagent" in result
    assert "AGENT_INSTANCE_ID=myagent-uuid" in result
    assert "HOME=/home/ubuntu" in result


def test_build_env_args_term_defaults(
    monkeypatch: pytest.MonkeyPatch, launch_svc: LaunchService
) -> None:
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    result = launch_svc._build_env_args("a", "b", "/h")
    assert "TERM=xterm-256color" in result
    assert "COLORTERM=truecolor" in result


def test_build_env_args_term_from_env(
    monkeypatch: pytest.MonkeyPatch, launch_svc: LaunchService
) -> None:
    monkeypatch.setenv("TERM", "screen")
    monkeypatch.setenv("COLORTERM", "16color")
    result = launch_svc._build_env_args("a", "b", "/h")
    assert "TERM=screen" in result
    assert "COLORTERM=16color" in result


def test_build_env_args_prompt_caching_unset(
    monkeypatch: pytest.MonkeyPatch, launch_svc: LaunchService
) -> None:
    monkeypatch.delenv("ENABLE_PROMPT_CACHING_1H", raising=False)
    result = launch_svc._build_env_args("a", "b", "/h")
    assert not any(arg.startswith("ENABLE_PROMPT_CACHING_1H=") for arg in result)


def test_build_env_args_prompt_caching_set(
    monkeypatch: pytest.MonkeyPatch, launch_svc: LaunchService
) -> None:
    monkeypatch.setenv("ENABLE_PROMPT_CACHING_1H", "1")
    result = launch_svc._build_env_args("a", "b", "/h")
    assert "ENABLE_PROMPT_CACHING_1H=1" in result


# --- build_volume_mounts ---


def test_build_volume_mounts_basic(tmp_path: Path, launch_svc: LaunchService) -> None:
    global_config = tmp_path / "config"
    global_config.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    tool = tmp_path / "tool"
    tool.mkdir()
    result = launch_svc._build_volume_mounts("/home/ubuntu")
    assert any(":/home/ubuntu/.claude.json" in v for v in result)
    assert any(":/home/ubuntu/.claude" in v for v in result)
    assert any(":/workspace" in v for v in result)
    assert any(":/home/ubuntu/.claude/projects/-workspace" in v for v in result)
    assert any(":/opt/agent-wrap:ro" in v for v in result)


# --- parse_dockerfile_directives ---


def test_parse_directives_no_dockerfile(tmp_path: Path, launch_svc: LaunchService) -> None:
    fake_dockerfile = tmp_path / "Dockerfile"
    user, ports, extras = launch_svc._parse_dockerfile_directives(fake_dockerfile)
    assert user == "ubuntu"
    assert ports == []
    assert extras == []


def test_parse_directives_with_dockerfile(tmp_path: Path, launch_svc: LaunchService) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text(
        "# agent-name: test\n"
        "# agent-user: customuser\n"
        "EXPOSE 8080\n"
        "# agent-run-args: --cap-add SYS_ADMIN\n"
    )
    user, ports, extras = launch_svc._parse_dockerfile_directives(dockerfile)
    assert user == "customuser"
    assert ports == ["-p", "127.0.0.1:8080:8080"]
    assert extras == ["--cap-add", "SYS_ADMIN"]


# --- resolve_host_network ---


def test_host_network_env_not_set(launch_svc: LaunchService) -> None:
    use, args, ports = launch_svc._resolve_host_network(None, [])
    assert use is False
    assert args == []
    assert ports == []


def test_host_network_not_wsl(
    monkeypatch: pytest.MonkeyPatch,
    mocker: pytest_mock.MockFixture,
    launch_svc: LaunchService,
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=False)
    use, _, _ = launch_svc._resolve_host_network(None, ["-p", "8080:8080"])
    assert use is False
    launch_svc._display.warning.assert_any_call(  # type: ignore[union-attr]
        "AGENT_USE_HOST_NETWORK ignored — only honored on WSL hosts."
    )


def test_host_network_wsl_no_agent_network(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=True)
    use, args, ports = launch_svc._resolve_host_network(None, ["-p", "8080:8080"])
    assert use is True
    assert args == ["--network", "host"]
    assert ports == []


def test_host_network_wsl_agent_network_specified(
    monkeypatch: pytest.MonkeyPatch,
    mocker: pytest_mock.MockFixture,
    launch_svc: LaunchService,
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.lib.docker_utils.is_wsl", return_value=True)
    use, _, ports = launch_svc._resolve_host_network("mynet", ["-p", "8080:8080"])
    assert use is False
    launch_svc._display.warning.assert_any_call(  # type: ignore[union-attr]
        "AGENT_USE_HOST_NETWORK ignored — Dockerfile.agent already "
        "specifies --network via agent-run-args."
    )
    assert ports == ["-p", "8080:8080"]


# --- collect_sidecars ---


def test_collect_sidecars_returns_provider_sidecars(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    # Sentinel placeholders — only compared for identity, no interface needed.
    sentinel = [mocker.Mock(spec=Sidecar), mocker.Mock(spec=Sidecar)]
    provider = mocker.Mock(spec=Provider)
    provider.sidecars.return_value = sentinel
    assert launch_svc._collect_sidecars(provider) == sentinel


# --- build_agent_labels ---


def test_build_agent_labels_empty_instance(launch_svc: LaunchService) -> None:
    assert launch_svc._build_agent_labels("") == []


def test_build_agent_labels_role_id_name_only(launch_svc: LaunchService) -> None:
    result = launch_svc._build_agent_labels("inst-1")
    assert "agent-wrap.role=claude-agent" in result
    assert "agent-wrap.instance-id=inst-1" in result
    assert not any(c.startswith("agent-wrap.sidecar.") for c in result)
    assert result.count("--name") == 1
    assert "claude-agent-inst-1" in result


# --- sidecar_lock_timeout ---


def test_sidecar_lock_timeout_sums_over_sidecars(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    a = mocker.Mock(spec=Sidecar, cold_start_time=120.0, short_circuit_time=2.0)
    b = mocker.Mock(spec=Sidecar, cold_start_time=30.0, short_circuit_time=1.0)
    assert launch_svc._sidecar_lock_timeout([a, b], 10) == 180.0


def test_sidecar_lock_timeout_zero_queue(
    mocker: pytest_mock.MockFixture, launch_svc: LaunchService
) -> None:
    a = mocker.Mock(spec=Sidecar, cold_start_time=120.0, short_circuit_time=2.0)
    assert launch_svc._sidecar_lock_timeout([a], 0) == 120.0


# --- extract_network ---


def test_extract_network_no_network(launch_svc: LaunchService) -> None:
    assert launch_svc._extract_network([]) is None
    assert launch_svc._extract_network(["--device", "/dev/fuse"]) is None


def test_extract_network_separate_flag(launch_svc: LaunchService) -> None:
    assert launch_svc._extract_network(["--network", "mynet"]) == "mynet"


def test_extract_network_equals_syntax(launch_svc: LaunchService) -> None:
    assert launch_svc._extract_network(["--network=mynet"]) == "mynet"


def test_extract_network_net_alias(launch_svc: LaunchService) -> None:
    assert launch_svc._extract_network(["--net", "mynet"]) == "mynet"
    assert launch_svc._extract_network(["--net=mynet"]) == "mynet"


def test_extract_network_first_occurrence_wins(launch_svc: LaunchService) -> None:
    assert launch_svc._extract_network(["--network", "first", "--network", "second"]) == "first"


def test_extract_network_missing_value(launch_svc: LaunchService) -> None:
    assert launch_svc._extract_network(["--network"]) is None


def test_extract_network_among_other_flags(launch_svc: LaunchService) -> None:
    args = ["--device", "/dev/fuse", "--network", "mynet", "--cap-add", "SYS_ADMIN"]
    assert launch_svc._extract_network(args) == "mynet"
