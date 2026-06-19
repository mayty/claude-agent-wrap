# This file has been edited with the assistance of an AI tool.
"""Tests for internal helpers in agent_wrap/commands/run.py."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.commands import run as run_mod
from agent_wrap.commands.run import (
    _build_env_args,
    _build_volume_mounts,
    _build_wslg_args,
    _is_wsl,
    _load_secrets,
    _load_telegram_creds,
    _parse_dockerfile_directives,
    _release_sidecars,
    _resolve_agent_name,
    _resolve_host_network,
    build_agent_labels,
    collect_sidecars,
    sidecar_lock_timeout,
)
from agent_wrap.commands.run import (
    run as agent_run,
)
from agent_wrap.lib.utils import ResolvedImage
from agent_wrap.sidecars import SidecarTracker

# --- _is_wsl ---


def test_is_wsl_true(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.commands.run.Path", autospec=True)
    mock_path.return_value.read_text.return_value = "Linux version 5.15 (microsoft)"
    assert _is_wsl() is True


def test_is_wsl_false(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.commands.run.Path", autospec=True)
    mock_path.return_value.read_text.return_value = "Linux version 5.15 (generic)"
    assert _is_wsl() is False


def test_is_wsl_os_error(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.commands.run.Path", autospec=True)
    mock_path.return_value.read_text.side_effect = OSError("no file")
    assert _is_wsl() is False


# --- _resolve_agent_name ---


def test_resolve_agent_name_use_base(tmp_path: Path) -> None:
    result = _resolve_agent_name(use_base=True, cwd=tmp_path)
    assert result == tmp_path.name.lower()


def test_resolve_agent_name_no_dockerfile(tmp_path: Path) -> None:
    result = _resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == tmp_path.name.lower()


def test_resolve_agent_name_from_dockerfile(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: my-custom-agent\nFROM claude-agent\n")
    result = _resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == "my-custom-agent"


def test_resolve_agent_name_dockerfile_no_agent_name(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("FROM claude-agent\n")
    result = _resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == tmp_path.name.lower()


def test_resolve_agent_name_empty_sanitized(tmp_path: Path) -> None:
    bad_dir = tmp_path / "---"
    bad_dir.mkdir()
    result = _resolve_agent_name(use_base=True, cwd=bad_dir)
    assert result == "agent"


def test_resolve_agent_name_empty_value_after_colon(tmp_path: Path) -> None:
    """Dockerfile with '# agent-name:' but no value falls back to dir name."""
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: \nFROM claude-agent\n")
    result = _resolve_agent_name(use_base=False, cwd=tmp_path)
    assert result == tmp_path.name.lower()


# --- _load_secrets ---


def test_load_secrets_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        _load_secrets()
    assert exc.value.code == 1


def test_load_secrets_invalid_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "claude_keys.json").write_text("{not json}")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with pytest.raises(SystemExit) as exc:
        _load_secrets()
    assert exc.value.code == 1


def test_load_secrets_with_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = {
        "ServiceSpecificCredential": {"ServiceCredentialSecret": "aws-key"},
        "TelegramBotToken": "123:ABC",
        "TelegramChatId": "456",
    }
    (tmp_path / "claude_keys.json").write_text(json.dumps(creds))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    token, chat_id = _load_secrets()
    assert token == "123:ABC"
    assert chat_id == "456"


def test_load_secrets_without_telegram(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    creds = {"ServiceSpecificCredential": {"ServiceCredentialSecret": "aws-key"}}
    (tmp_path / "claude_keys.json").write_text(json.dumps(creds))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    token, chat_id = _load_secrets()
    assert token == ""
    assert chat_id == ""


# --- _load_telegram_creds ---


@pytest.mark.parametrize(
    ("secrets", "expected"),
    [
        ({"TelegramBotToken": "abc", "TelegramChatId": "123"}, ("abc", "123")),
        ({}, ("", "")),
        ({"TelegramBotToken": None, "TelegramChatId": None}, ("", "")),
    ],
)
def test_load_telegram_creds(secrets: dict, expected: tuple[str, str]) -> None:
    assert _load_telegram_creds(secrets) == expected


# --- _build_wslg_args ---


def test_build_wslg_args_not_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mnt = tmp_path / "mnt" / "wslg"
    # Don't create the directory so is_dir() returns False
    monkeypatch.setattr(
        "agent_wrap.commands.run.Path",
        lambda path: fake_mnt if str(path) == "/mnt/wslg" else Path(path),
    )
    result = _build_wslg_args(Path("/tool"))
    assert result == []


def test_build_wslg_args_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mnt = tmp_path / "mnt" / "wslg"
    fake_mnt.mkdir(parents=True)
    monkeypatch.setattr(
        "agent_wrap.commands.run.Path",
        lambda path: fake_mnt if str(path) == "/mnt/wslg" else Path(path),
    )
    result = _build_wslg_args(Path("/tool"))
    assert "-v" in result
    assert "/mnt/wslg/runtime-dir:/mnt/wslg/runtime-dir" in result
    assert "/mnt/wslg/.X11-unix:/tmp/.X11-unix" in result
    # The full tree must NOT be mounted — /mnt/wslg/distro is the host root filesystem.
    assert "/mnt/wslg:/mnt/wslg" not in result
    assert f"{Path('/tool')}/ops/wl-paste-shim:/usr/local/bin/wl-paste:ro" in result
    assert "-e" in result
    assert "DISPLAY" in result
    assert "WAYLAND_DISPLAY" in result
    assert "XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir" in result


# --- _build_env_args ---


def test_build_env_args_basic() -> None:
    result = _build_env_args("token123", "chat456", "myagent", "myagent-uuid", "/home/ubuntu")
    assert "-e" in result
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" in result
    assert "TELEGRAM_BOT_TOKEN=token123" in result
    assert "TELEGRAM_CHAT_ID=chat456" in result
    assert "AGENT_NAME=myagent" in result
    assert "AGENT_INSTANCE_ID=myagent-uuid" in result
    assert "HOME=/home/ubuntu" in result


def test_build_env_args_term_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("COLORTERM", raising=False)
    result = _build_env_args("", "", "a", "b", "/h")
    assert "TERM=xterm-256color" in result
    assert "COLORTERM=truecolor" in result


def test_build_env_args_term_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "screen")
    monkeypatch.setenv("COLORTERM", "16color")
    result = _build_env_args("", "", "a", "b", "/h")
    assert "TERM=screen" in result
    assert "COLORTERM=16color" in result


# --- _build_volume_mounts ---


def test_build_volume_mounts_basic(tmp_path: Path) -> None:
    global_config = tmp_path / "config"
    global_config.mkdir()
    cwd = tmp_path / "project"
    cwd.mkdir()
    tool = tmp_path / "tool"
    tool.mkdir()
    result = _build_volume_mounts(global_config, cwd, tool, "/home/ubuntu")
    assert f"{global_config}/.claude.json:/home/ubuntu/.claude.json" in result
    assert f"{global_config}/.claude:/home/ubuntu/.claude" in result
    assert f"{cwd}:/workspace" in result
    assert f"{cwd}/.claude/sessions:/home/ubuntu/.claude/projects/-workspace" in result
    assert f"{tool}/ops:/opt/agent-wrap:ro" in result


# --- _parse_dockerfile_directives ---


def test_parse_directives_no_dockerfile(tmp_path: Path) -> None:
    fake_dockerfile = tmp_path / "Dockerfile"
    user, ports, extras = _parse_dockerfile_directives(fake_dockerfile)
    assert user == "ubuntu"
    assert ports == []
    assert extras == []


def test_parse_directives_with_dockerfile(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text(
        "# agent-name: test\n"
        "# agent-user: customuser\n"
        "EXPOSE 8080\n"
        "# agent-run-args: --cap-add SYS_ADMIN\n"
    )
    user, ports, extras = _parse_dockerfile_directives(dockerfile)
    assert user == "customuser"
    assert ports == ["-p", "127.0.0.1:8080:8080"]
    assert extras == ["--cap-add", "SYS_ADMIN"]


# --- _resolve_host_network ---


def test_host_network_env_not_set() -> None:
    use, args, ports = _resolve_host_network(None, [])
    assert use is False
    assert args == []
    assert ports == []


def test_host_network_not_wsl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.commands.run._is_wsl", return_value=False)
    use, _, _ = _resolve_host_network(None, ["-p", "8080:8080"])
    assert use is False
    assert "only honored on WSL" in capsys.readouterr().err


def test_host_network_wsl_no_agent_network(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.commands.run._is_wsl", return_value=True)
    use, args, ports = _resolve_host_network(None, ["-p", "8080:8080"])
    assert use is True
    assert args == ["--network", "host"]
    assert ports == []


def test_host_network_wsl_agent_network_specified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.commands.run._is_wsl", return_value=True)
    use, _, ports = _resolve_host_network("mynet", ["-p", "8080:8080"])
    assert use is False
    assert "AGENT_USE_HOST_NETWORK ignored" in capsys.readouterr().err
    assert ports == ["-p", "8080:8080"]


# --- run() ---


def test_run_image_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    mocker: pytest_mock.MockFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    mocker.patch("agent_wrap.commands.update.check", return_value=False)
    mocker.patch("agent_wrap.commands.run.resolve_image").return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mocker.patch("agent_wrap.commands.run.docker_utils.image_exists", return_value=False)
    mocker.patch("agent_wrap.commands.run._load_secrets", return_value=("", ""))
    rc = agent_run([], tmp_path)
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_run_resolve_image_exits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    mocker: pytest_mock.MockFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    mocker.patch("agent_wrap.commands.update.check", return_value=False)
    mocker.patch(
        "agent_wrap.commands.run.resolve_image", side_effect=SystemExit("no Dockerfile.agent")
    )
    rc = agent_run([], tmp_path)
    assert rc == 1
    assert "no Dockerfile.agent" in capsys.readouterr().err


def test_run_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    # Create claude_keys.json
    keys = tmp_path / "claude_keys.json"
    keys.write_text(json.dumps({"ServiceSpecificCredential": {"ServiceCredentialSecret": "key"}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    # Create a minimal Dockerfile.agent
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: test\nFROM claude-agent\n")

    mocker.patch("agent_wrap.commands.update.check", return_value=False)
    mocker.patch("agent_wrap.commands.run.resolve_image").return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mocker.patch("agent_wrap.commands.run.docker_utils.image_exists", return_value=True)
    mocker.patch("agent_wrap.commands.run.docker_utils.get_user_args", return_value=[])
    mocker.patch("agent_wrap.commands.run.docker_utils.get_tty_args", return_value=["-it"])
    mocker.patch("agent_wrap.commands.run.config.prepare_global_config")
    mocker.patch("agent_wrap.commands.run.config.prepare_project_dirs")
    mocker.patch("agent_wrap.commands.run.config.record_project")
    mocker.patch("agent_wrap.commands.run.generate_uuid", return_value="test-uuid")

    # Mock provider with a single sidecar.
    mock_sidecar = mocker.MagicMock()
    mock_sidecar.ensure.return_value = []
    mock_sidecar.cold_start_time = 120.0
    mock_sidecar.short_circuit_time = 2.0
    mock_provider = mocker.MagicMock()
    mock_provider.sidecars.return_value = [mock_sidecar]
    mocker.patch("agent_wrap.commands.run.get_provider", return_value=mock_provider)

    mock_result = mocker.MagicMock()
    mock_result.returncode = 0
    mock_run = mocker.patch("agent_wrap.commands.run.subprocess.run", return_value=mock_result)

    rc = agent_run(["--base"], tmp_path)
    assert rc == 0
    assert mock_sidecar.prepare.call_count == 1
    assert mock_sidecar.ensure.call_count == 1
    assert mock_sidecar.release.call_count == 1
    assert mock_run.called
    # Verify the docker run command structure
    cmd = mock_run.call_args[0][0]
    assert cmd[0] == "docker"
    assert cmd[1] == "run"
    assert "--rm" in cmd
    assert "-it" in cmd
    assert "claude-agent-test" in cmd
    assert any("AGENT_INSTANCE_ID=" in v for v in cmd)
    assert any("/workspace" in v for v in cmd if isinstance(v, str))
    # One common role label; NO per-sidecar label.
    assert "agent-wrap.role=claude-agent" in cmd
    assert not any(c.startswith("agent-wrap.sidecar.") for c in cmd if isinstance(c, str))


def test_run_prepares_config_inside_single_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """Config prep runs inside the ONE launch-lock acquisition (no double-acquire)."""
    monkeypatch.chdir(tmp_path)
    keys = tmp_path / "claude_keys.json"
    keys.write_text(json.dumps({"ServiceSpecificCredential": {"ServiceCredentialSecret": "key"}}))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: test\nFROM claude-agent\n")

    mocker.patch("agent_wrap.commands.update.check", return_value=False)
    mocker.patch("agent_wrap.commands.run.resolve_image").return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mocker.patch("agent_wrap.commands.run.docker_utils.image_exists", return_value=True)
    mocker.patch("agent_wrap.commands.run.docker_utils.get_user_args", return_value=[])
    mocker.patch("agent_wrap.commands.run.docker_utils.get_tty_args", return_value=["-it"])
    mocker.patch("agent_wrap.commands.run.config.prepare_project_dirs")
    mocker.patch("agent_wrap.commands.run.config.record_project")
    mocker.patch("agent_wrap.commands.run.config.link_litellm_logs")
    mocker.patch("agent_wrap.commands.run.generate_uuid", return_value="test-uuid")

    mock_provider = mocker.MagicMock()
    mock_provider.sidecars.return_value = []
    mocker.patch("agent_wrap.commands.run.get_provider", return_value=mock_provider)
    mock_result = mocker.MagicMock()
    mock_result.returncode = 0
    mocker.patch("agent_wrap.commands.run.subprocess.run", return_value=mock_result)

    # Record the interleaving of lock enter/exit with the config-prep call.
    lock_name = SidecarTracker(tmp_path).lock_path.name
    events: list[str] = []
    real_file_lock = run_mod.file_lock

    @contextmanager
    def tracking_file_lock(path: Path, *, timeout: float | None = None, poll: float = 0.1):  # type: ignore[no-untyped-def]
        events.append(f"lock-enter:{path.name}")
        with real_file_lock(path, timeout=timeout, poll=poll):
            yield
        events.append(f"lock-exit:{path.name}")

    mocker.patch("agent_wrap.commands.run.file_lock", side_effect=tracking_file_lock)
    mocker.patch(
        "agent_wrap.commands.run.config.prepare_global_config",
        side_effect=lambda *a, **k: events.append("prepare-global-config"),
    )

    rc = agent_run(["--base"], tmp_path)
    assert rc == 0

    # Exactly ONE acquisition of the launch lock (regression guard: no double-acquire),
    # and config prep happens between that single enter and its exit.
    assert events.count(f"lock-enter:{lock_name}") == 1
    enter = events.index(f"lock-enter:{lock_name}")
    exit_ = events.index(f"lock-exit:{lock_name}")
    prep = events.index("prepare-global-config")
    assert enter < prep < exit_


# --- collect_sidecars ---


def test_collect_sidecars_returns_provider_sidecars(mocker: pytest_mock.MockFixture) -> None:
    sentinel = [mocker.MagicMock(), mocker.MagicMock()]
    provider = mocker.MagicMock()
    provider.sidecars.return_value = sentinel
    assert collect_sidecars(provider) == sentinel


# --- build_agent_labels ---


def test_build_agent_labels_empty_instance() -> None:
    assert build_agent_labels("") == []


def test_build_agent_labels_role_id_name_only() -> None:
    result = build_agent_labels("inst-1")
    assert "agent-wrap.role=claude-agent" in result
    assert "agent-wrap.instance-id=inst-1" in result
    # No per-sidecar label — the tracker counts all agents by role.
    assert not any(c.startswith("agent-wrap.sidecar.") for c in result)
    assert result.count("--name") == 1
    assert "claude-agent-inst-1" in result


# --- sidecar_lock_timeout ---


def test_sidecar_lock_timeout_sums_over_sidecars(mocker: pytest_mock.MockFixture) -> None:
    a = mocker.MagicMock(cold_start_time=120.0, short_circuit_time=2.0)
    b = mocker.MagicMock(cold_start_time=30.0, short_circuit_time=1.0)
    # Σ(cold_start + X·short_circuit): (120+10·2) + (30+10·1) = 140 + 40 = 180
    assert sidecar_lock_timeout([a, b], 10) == 180.0


def test_sidecar_lock_timeout_zero_queue(mocker: pytest_mock.MockFixture) -> None:
    a = mocker.MagicMock(cold_start_time=120.0, short_circuit_time=2.0)
    assert sidecar_lock_timeout([a], 0) == 120.0


# --- lifecycle: ensure-all / release-all ---


def _run_with_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mocker: pytest_mock.MockFixture,
    sidecars: list,
) -> None:
    """
    Drive run() to the lifecycle block with a provider declaring *sidecars*.

    The real SidecarTracker is used, operating on its lock-file registries under
    ``tmp_path/.agent-launches``; with no other run registered, teardown proceeds.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "claude_keys.json").write_text(
        json.dumps({"ServiceSpecificCredential": {"ServiceCredentialSecret": "key"}})
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: test\nFROM claude-agent\n")

    mocker.patch("agent_wrap.commands.update.check", return_value=False)
    mocker.patch("agent_wrap.commands.run.resolve_image").return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mocker.patch("agent_wrap.commands.run.docker_utils.image_exists", return_value=True)
    mocker.patch("agent_wrap.commands.run.docker_utils.get_user_args", return_value=[])
    mocker.patch("agent_wrap.commands.run.docker_utils.get_tty_args", return_value=["-it"])
    mocker.patch("agent_wrap.commands.run.config.prepare_global_config")
    mocker.patch("agent_wrap.commands.run.config.prepare_project_dirs")
    mocker.patch("agent_wrap.commands.run.config.record_project")
    mocker.patch("agent_wrap.commands.run.generate_uuid", return_value="test-uuid")

    provider = mocker.MagicMock()
    provider.sidecars.return_value = sidecars
    mocker.patch("agent_wrap.commands.run.get_provider", return_value=provider)

    result = mocker.MagicMock()
    result.returncode = 0
    mocker.patch("agent_wrap.commands.run.subprocess.run", return_value=result)


def _sidecar_mock(mocker: pytest_mock.MockFixture, key: str, order: list[str] | None = None):
    """Return a MagicMock Sidecar with timing knobs + optional ordered lifecycle."""
    sc = mocker.MagicMock()
    sc.cold_start_time = 120.0
    sc.short_circuit_time = 2.0
    if order is None:
        sc.ensure.return_value = []
    else:
        sc.prepare.side_effect = lambda: order.append(f"prepare-{key}")
        sc.ensure.side_effect = lambda **_: order.append(f"ensure-{key}") or []
        sc.release.side_effect = lambda: order.append(f"release-{key}")
    return sc


def test_run_prepares_all_before_ensure_then_releases_reverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    order: list[str] = []
    a = _sidecar_mock(mocker, "a", order)
    b = _sidecar_mock(mocker, "b", order)

    _run_with_sidecars(tmp_path, monkeypatch, mocker, [a, b])
    agent_run(["--base"], tmp_path)

    # All prepare() (lock-free) before any ensure(); ensured in order; released reverse.
    assert order == [
        "prepare-a",
        "prepare-b",
        "ensure-a",
        "ensure-b",
        "release-b",
        "release-a",
    ]


def test_run_skips_release_when_another_runner_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """When another run's running registration is still held, no sidecar is released."""
    a = _sidecar_mock(mocker, "a")
    _run_with_sidecars(tmp_path, monkeypatch, mocker, [a])
    # A concurrent agent holds its running registration for the whole of our run.
    other = SidecarTracker(tmp_path)
    other_handle = other.register_running("other-inst")
    try:
        agent_run(["--base"], tmp_path)
        a.release.assert_not_called()
    finally:
        other.clear_running(other_handle, "other-inst")


def test_run_releases_when_no_other_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """With no other run registered, the finishing run is last out and tears down."""
    a = _sidecar_mock(mocker, "a")
    _run_with_sidecars(tmp_path, monkeypatch, mocker, [a])
    agent_run(["--base"], tmp_path)
    a.release.assert_called_once_with()


def test_release_yields_to_live_waiter_then_proceeds(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    """A stopping run yields the lock while a starter waits, then tears down once gone."""
    tracker = SidecarTracker(tmp_path)
    a = _sidecar_mock(mocker, "a")
    # A starter holds its waiter ticket; the stopper must yield (loop) until it clears.
    waiter_handle = tracker.register_waiter("starter-inst")

    sleeps: list[float] = []

    def _release_on_third_sleep(_secs: float) -> None:
        sleeps.append(_secs)
        # After two yields, the starter finishes waiting and clears its ticket.
        if len(sleeps) == 2:
            tracker.clear_waiter(waiter_handle, "starter-inst")

    mocker.patch("agent_wrap.commands.run.time.sleep", side_effect=_release_on_third_sleep)

    _release_sidecars([a], tracker, "stopper-inst", running_handle=None)

    # It looped (yielded) while the waiter was live, then released once it cleared.
    assert len(sleeps) == 2
    a.release.assert_called_once_with()


def test_run_uses_summed_lock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """ensure-all is wrapped in file_lock with the summed timeout."""
    a = _sidecar_mock(mocker, "a")  # cold 120, short 2
    _run_with_sidecars(tmp_path, monkeypatch, mocker, [a])
    fl = mocker.patch("agent_wrap.commands.run.file_lock")
    monkeypatch.setenv("AGENT_EXPECTED_QUEUE_DEPTH", "10")
    agent_run(["--base"], tmp_path)
    # The ensure-all lock is the timed one (the release lock blocks with no timeout).
    timeouts = [c.kwargs["timeout"] for c in fl.call_args_list if "timeout" in c.kwargs]
    # 120 + 10·2 = 140
    assert timeouts == [140.0]


def test_run_partial_ensure_failure_releases_full_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """If sidecar #2's ensure raises, the FULL declared set is still released."""
    a = _sidecar_mock(mocker, "a")
    b = _sidecar_mock(mocker, "b")
    b.ensure.side_effect = SystemExit("boom")

    _run_with_sidecars(tmp_path, monkeypatch, mocker, [a, b])
    with pytest.raises(SystemExit):
        agent_run(["--base"], tmp_path)

    # Last-light-out releases every declared sidecar (release is a no-op when a
    # container isn't running), so no orphan is left behind.
    assert a.release.call_count == 1
    assert b.release.call_count == 1


def test_run_releases_sidecars_ensure_never_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """release() iterates the full declared set, even sidecars ensure() never reached."""
    # `a`'s ensure raises first, so the loop never calls `b.ensure()` at all. `b` could
    # still be an orphan a prior run left running, so it must be released regardless.
    a = _sidecar_mock(mocker, "a")
    b = _sidecar_mock(mocker, "b")
    a.ensure.side_effect = SystemExit("boom on first")

    _run_with_sidecars(tmp_path, monkeypatch, mocker, [a, b])
    with pytest.raises(SystemExit):
        agent_run(["--base"], tmp_path)

    b.ensure.assert_not_called()  # the ensure loop never reached b
    b.release.assert_called_once_with()  # ...yet last-light-out still releases it
    a.release.assert_called_once_with()
