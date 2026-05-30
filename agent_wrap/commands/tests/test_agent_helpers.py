# This file has been created with the assistance of an AI tool.
"""Tests for internal helpers in agent_wrap/commands/agent.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_mock

from agent_wrap.commands.agent import (
    _build_env_args,
    _build_volume_mounts,
    _build_wslg_args,
    _is_wsl,
    _load_secrets,
    _load_telegram_creds,
    _parse_dockerfile_directives,
    _resolve_agent_name,
    _resolve_host_network,
)

# --- _is_wsl ---


def test_is_wsl_true(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    proc = tmp_path / "proc_version"
    proc.write_text("Linux version 5.15 (microsoft)")
    mock_path = mocker.patch("agent_wrap.commands.agent.Path")
    mock_instance = MagicMock()
    mock_instance.read_text.return_value = "Linux version 5.15 (microsoft)"
    mock_path.return_value = mock_instance
    assert _is_wsl() is True


def test_is_wsl_false(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.commands.agent.Path")
    mock_instance = MagicMock()
    mock_instance.read_text.return_value = "Linux version 5.15 (generic)"
    mock_path.return_value = mock_instance
    assert _is_wsl() is False


def test_is_wsl_os_error(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.commands.agent.Path")
    mock_instance = MagicMock()
    mock_instance.read_text.side_effect = OSError("no file")
    mock_path.return_value = mock_instance
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


def test_load_telegram_creds_with_values() -> None:
    result = _load_telegram_creds({"TelegramBotToken": "abc", "TelegramChatId": "123"})
    assert result == ("abc", "123")


def test_load_telegram_creds_missing() -> None:
    result = _load_telegram_creds({})
    assert result == ("", "")


def test_load_telegram_creds_none_values() -> None:
    result = _load_telegram_creds({"TelegramBotToken": None, "TelegramChatId": None})
    assert result == ("", "")


# --- _build_wslg_args ---


def test_build_wslg_args_not_present(mocker: pytest_mock.MockFixture) -> None:
    mock_path = mocker.patch("agent_wrap.commands.agent.Path")
    mock_path.return_value = MagicMock()
    mock_path.return_value.is_dir.return_value = False
    result = _build_wslg_args(Path("/tool"))
    assert result == []


def test_build_wslg_args_present(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch("pathlib.Path.is_dir", return_value=True)
    result = _build_wslg_args(Path("/tool"))
    assert len(result) > 0
    assert "-v" in result
    assert "/mnt/wslg:/mnt/wslg" in result
    assert "-e" in result
    assert "DISPLAY" in result


# --- _build_env_args ---


def test_build_env_args_basic() -> None:
    result = _build_env_args("token123", "chat456", "myagent", "myagent-uuid", "/home/ubuntu")
    assert "-e" in result
    assert "DISABLE_AUTOUPDATER=1" in result
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
    assert f"{cwd}/.claude/sessions:/home/ubuntu/.claude/sessions" in result
    assert f"{tool}/Dockerfile:/opt/agent-wrap/Dockerfile:ro" in result


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
    mocker.patch("agent_wrap.commands.agent._is_wsl", return_value=False)
    use, _, _ = _resolve_host_network(None, ["-p", "8080:8080"])
    assert use is False
    assert "only honored on WSL" in capsys.readouterr().err


def test_host_network_wsl_no_agent_network(
    monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.commands.agent._is_wsl", return_value=True)
    use, args, ports = _resolve_host_network(None, ["-p", "8080:8080"])
    assert use is True
    assert args == ["--network", "host"]
    assert ports == []


def test_host_network_wsl_agent_network_specified(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    monkeypatch.setenv("AGENT_USE_HOST_NETWORK", "1")
    mocker.patch("agent_wrap.commands.agent._is_wsl", return_value=True)
    use, _, ports = _resolve_host_network("mynet", ["-p", "8080:8080"])
    assert use is False
    assert "AGENT_USE_HOST_NETWORK ignored" in capsys.readouterr().err
    assert ports == ["-p", "8080:8080"]
