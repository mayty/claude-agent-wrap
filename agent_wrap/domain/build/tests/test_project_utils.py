# This file has been edited with the assistance of an AI tool.
"""Tests for BuildService.parse_dockerfile_agent and BuildService.resolve_image."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.updates.service import UpdateService

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def build_svc() -> BuildService:
    """Return a BuildService with a no-op update service."""
    return BuildService(
        update_service=Mock(spec=UpdateService),
        display_service=Mock(spec=DisplayService),
    )


# --- parse_dockerfile_agent ---


def test_agent_user(write_dockerfile: Callable[[str], Path], build_svc: BuildService):
    p = write_dockerfile("# agent-name: test\n# agent-user: customuser\nFROM claude-agent\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.agent_user == "customuser"


def test_default_agent_user(write_dockerfile: Callable[[str], Path], build_svc: BuildService):
    p = write_dockerfile("# agent-name: test\nFROM claude-agent\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.agent_user == "ubuntu"


def test_expose_ports(write_dockerfile: Callable[[str], Path], build_svc: BuildService):
    p = write_dockerfile("FROM claude-agent\nEXPOSE 8080 3000/tcp\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.expose_ports == ["8080", "3000"]


def test_agent_run_args(write_dockerfile: Callable[[str], Path], build_svc: BuildService):
    p = write_dockerfile(
        "FROM claude-agent\n# agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN\n"
    )
    info = build_svc.parse_dockerfile_agent(p)
    assert info.extra_run_args == ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"]


def test_multiple_run_args_lines(write_dockerfile: Callable[[str], Path], build_svc: BuildService):
    p = write_dockerfile(
        "FROM claude-agent\n"
        "# agent-run-args: --device /dev/fuse\n"
        "# agent-run-args: --cap-add SYS_ADMIN\n"
    )
    info = build_svc.parse_dockerfile_agent(p)
    assert info.extra_run_args == ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"]


def test_empty_dockerfile(write_dockerfile: Callable[[str], Path], build_svc: BuildService):
    p = write_dockerfile("FROM claude-agent\n")
    info = build_svc.parse_dockerfile_agent(p)
    assert info.agent_user == "ubuntu"
    assert info.expose_ports == []
    assert info.extra_run_args == []


def test_parse_nonexistent_file(build_svc: BuildService):
    with pytest.raises(FileNotFoundError):
        build_svc.parse_dockerfile_agent(Path("/nonexistent/Dockerfile.agent"))


# --- resolve_image ---


def test_resolve_base_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    result = build_svc.resolve_image(use_base=True)
    assert result.image == "claude-agent"
    assert result.dockerfile == tmp_path / "ops" / "Dockerfile"
    assert result.context == tmp_path


def test_resolve_with_dockerfile_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: myproj\nFROM claude-agent\n")
    result = build_svc.resolve_image(use_base=False)
    assert result.image == "claude-agent-myproj"
    assert result.dockerfile == tmp_path / "Dockerfile.agent"
    assert result.context == tmp_path


def test_resolve_base_ignores_dockerfile_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: myproj\nFROM claude-agent\n")
    result = build_svc.resolve_image(use_base=True)
    assert result.image == "claude-agent"


def test_resolve_no_agent_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("FROM claude-agent\n")
    with pytest.raises(SystemExit, match="must contain '# agent-name:"):
        build_svc.resolve_image(use_base=False)


def test_resolve_invalid_agent_name_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("# agent-name: UPPER CASE\nFROM claude-agent\n")
    with pytest.raises(SystemExit, match="must match"):
        build_svc.resolve_image(use_base=False)


def test_resolve_no_dockerfile_uses_base(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_svc: BuildService
) -> None:
    monkeypatch.chdir(tmp_path)
    result = build_svc.resolve_image(use_base=False)
    assert result.image == "claude-agent"
