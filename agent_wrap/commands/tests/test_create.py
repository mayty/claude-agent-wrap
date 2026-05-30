# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/commands/create.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from agent_wrap.commands.create import run as create_run


def test_creates_dockerfile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    rc = create_run([], tmp_path)
    assert rc == 0
    dockerfile = tmp_path / "Dockerfile.agent"
    assert dockerfile.exists()
    content = dockerfile.read_text()
    assert f"# agent-name: {tmp_path.name.lower()}" in content
    assert "FROM claude-agent" in content


def test_refuses_if_already_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("FROM claude-agent\n")
    rc = create_run([], tmp_path)
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_empty_sanitized_name_returns_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    dir_with_bad_name = tmp_path / "---"
    dir_with_bad_name.mkdir()
    monkeypatch.chdir(dir_with_bad_name)
    with patch("pathlib.Path.cwd", return_value=dir_with_bad_name):
        rc = create_run([], tmp_path)
    assert rc == 1
    assert "could not derive agent-name" in capsys.readouterr().err
