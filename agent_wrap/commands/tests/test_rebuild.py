# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/commands/rebuild.py."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.commands.rebuild import _check_from_line, _do_rebuild, _parse_rebuild_args
from agent_wrap.utils import ResolvedImage

# --- _parse_rebuild_args ---


def test_no_args() -> None:
    assert _parse_rebuild_args([]) == (False, False)


def test_full_flag() -> None:
    assert _parse_rebuild_args(["--full"]) == (True, False)


def test_help_short(capsys: pytest.CaptureFixture) -> None:
    result = _parse_rebuild_args(["--help"])
    assert result == (False, True)
    out = capsys.readouterr().out
    assert "Usage: rebuild_agent" in out


def test_help_long(capsys: pytest.CaptureFixture) -> None:
    result = _parse_rebuild_args(["-h"])
    assert result == (False, True)
    assert "Usage: rebuild_agent" in capsys.readouterr().out


def test_full_and_help(capsys: pytest.CaptureFixture) -> None:
    result = _parse_rebuild_args(["--full", "--help"])
    assert result == (True, True)


def test_unknown_arg(capsys: pytest.CaptureFixture) -> None:
    result = _parse_rebuild_args(["--unknown"])
    assert result == (False, True)
    assert "unknown argument" in capsys.readouterr().err


# --- _check_from_line ---


def test_from_claude_agent_image_exists(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM claude-agent\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mocker.patch("agent_wrap.commands.rebuild.image_exists", return_value=True)
    assert _check_from_line(resolved) is True


def test_from_claude_agent_image_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM claude-agent\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mocker.patch("agent_wrap.commands.rebuild.image_exists", return_value=False)
    assert _check_from_line(resolved) is False
    assert "base image is not built" in capsys.readouterr().err


def test_from_custom_image(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: test\nFROM ubuntu:24.04\n")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert _check_from_line(resolved) is True
    assert "inherits from" in capsys.readouterr().err


def test_empty_dockerfile(tmp_path: Path) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("")
    resolved = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    assert _check_from_line(resolved) is True


# --- _do_rebuild ---


def test_do_rebuild_resolve_image_exit(
    tmp_path: Path, capsys: pytest.CaptureFixture, mocker: pytest_mock.MockFixture
) -> None:
    mocker.patch(
        "agent_wrap.commands.rebuild.resolve_image", side_effect=SystemExit("no Dockerfile.agent")
    )
    rc = _do_rebuild(tmp_path, full=False)
    assert rc == 1
    assert "no Dockerfile.agent" in capsys.readouterr().err


def test_do_rebuild_full_build_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_resolve = mocker.patch("agent_wrap.commands.rebuild.resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.commands.rebuild.subprocess.run")
    mock_run.return_value.returncode = 1
    rc = _do_rebuild(tmp_path, full=True)
    assert rc == 1


def test_do_rebuild_project_build_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    dockerfile = tmp_path / "Dockerfile.agent"
    dockerfile.write_text("# agent-name: t\nFROM custom-image\n")
    mock_resolve = mocker.patch("agent_wrap.commands.rebuild.resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=dockerfile,
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.commands.rebuild.subprocess.run")
    mock_run.return_value.returncode = 1
    rc = _do_rebuild(tmp_path, full=False)
    assert rc == 1


def test_do_rebuild_check_from_line_fails(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    mock_resolve = mocker.patch("agent_wrap.commands.rebuild.resolve_image")
    mock_resolve.return_value = ResolvedImage(
        image="claude-agent-test",
        dockerfile=tmp_path / "Dockerfile.agent",
        context=tmp_path,
    )
    mock_run = mocker.patch("agent_wrap.commands.rebuild.subprocess.run")
    mock_run.return_value.returncode = 0
    mocker.patch("agent_wrap.commands.rebuild.image_exists", return_value=False)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: t\nFROM claude-agent\n")
    rc = _do_rebuild(tmp_path, full=False)
    assert rc == 1
