# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.utils."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.utils import (
    generate_uuid,
    parse_dockerfile_agent,
    sanitize_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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


@pytest.fixture
def write_temp(tmp_path: Path) -> Callable[[str], Path]:
    """Write content to a temporary file and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "Dockerfile.agent"
        p.write_text(content)
        return p

    return _write


def test_agent_user(write_temp: Callable[[str], Path]):
    p = write_temp("# agent-name: test\n# agent-user: customuser\nFROM claude-agent\n")
    info = parse_dockerfile_agent(p)
    assert info.agent_user == "customuser"


def test_default_agent_user(write_temp: Callable[[str], Path]):
    p = write_temp("# agent-name: test\nFROM claude-agent\n")
    info = parse_dockerfile_agent(p)
    assert info.agent_user == "ubuntu"


def test_expose_ports(write_temp: Callable[[str], Path]):
    p = write_temp("FROM claude-agent\nEXPOSE 8080 3000/tcp\n")
    info = parse_dockerfile_agent(p)
    assert info.expose_ports == ["8080", "3000"]


def test_agent_run_args(write_temp: Callable[[str], Path]):
    p = write_temp("FROM claude-agent\n# agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN\n")
    info = parse_dockerfile_agent(p)
    assert info.extra_run_args == ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"]


def test_multiple_run_args_lines(write_temp: Callable[[str], Path]):
    p = write_temp(
        "FROM claude-agent\n"
        "# agent-run-args: --device /dev/fuse\n"
        "# agent-run-args: --cap-add SYS_ADMIN\n"
    )
    info = parse_dockerfile_agent(p)
    assert info.extra_run_args == ["--device", "/dev/fuse", "--cap-add", "SYS_ADMIN"]


def test_empty_dockerfile(write_temp: Callable[[str], Path]):
    p = write_temp("FROM claude-agent\n")
    info = parse_dockerfile_agent(p)
    assert info.agent_user == "ubuntu"
    assert info.expose_ports == []
    assert info.extra_run_args == []
