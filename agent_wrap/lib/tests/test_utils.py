# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.lib.utils."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.build.service import BuildService
from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.updates.service import UpdateService
from agent_wrap.lib.path_hash import project_path_hash
from agent_wrap.lib.utils import generate_uuid, is_truthy_env, sanitize_name

if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.fixture
def build_svc() -> BuildService:
    """Return a BuildService with mocked dependencies."""
    return BuildService(
        update_service=Mock(spec=UpdateService),
        display_service=Mock(spec=DisplayService),
    )


def test_lowercase():
    assert sanitize_name("Hello") == "hello"


def test_replace_spaces():
    assert sanitize_name("hello world") == "hello-world"


def test_replace_special_chars():
    assert sanitize_name("hello@world!") == "hello-world"


def test_strip_leading_trailing_dashes():
    assert sanitize_name("---hello---") == "hello"


def test_preserve_dots_underscores_dashes():
    assert sanitize_name("my-project_v2.0") == "my-project_v2.0"


def test_empty_after_sanitize():
    assert sanitize_name("---") == ""


def test_mixed_case_and_special():
    assert sanitize_name("My Project (v2)") == "my-project--v2"


def test_returns_string():
    assert isinstance(generate_uuid(), str)


def test_format():
    parts = generate_uuid().split("-")
    assert len(parts) == 5


def test_lowercase_uuid():
    result = generate_uuid()
    assert result == result.lower()


def test_unique():
    assert generate_uuid() != generate_uuid()


# --- project_path_hash ---


def test_project_path_hash_is_16_hex(tmp_path: Path) -> None:
    result = project_path_hash(tmp_path)
    assert len(result) == 16
    assert all(c in "0123456789abcdef" for c in result)


def test_project_path_hash_stable(tmp_path: Path) -> None:
    assert project_path_hash(tmp_path) == project_path_hash(tmp_path)


def test_project_path_hash_resolves_symlink_aliases(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real)
    # A symlink alias and its target resolve to the same path -> same hash.
    assert project_path_hash(link) == project_path_hash(real)


def test_project_path_hash_differs_by_path(tmp_path: Path) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert project_path_hash(a) != project_path_hash(b)


# --- is_truthy_env ---


@pytest.mark.parametrize("value", ["", "0", "false", "no", "FALSE", "NO", "False"])
def test_is_truthy_env_false(value: str) -> None:
    assert is_truthy_env(value) is False


@pytest.mark.parametrize("value", ["1", "yes", "YES", "true", "TRUE", "anything"])
def test_is_truthy_env_true(value: str) -> None:
    assert is_truthy_env(value) is True


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
