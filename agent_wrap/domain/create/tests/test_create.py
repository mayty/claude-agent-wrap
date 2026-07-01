# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.domain.create.create.CreateService."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_wrap.domain.create.service import CreateService


@pytest.fixture
def svc() -> CreateService:
    return CreateService()


def test_create_writes_dockerfile(
    tmp_path: Path, svc: CreateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = svc.create()
    assert rc == 0
    dockerfile = tmp_path / "Dockerfile.agent"
    assert dockerfile.exists()
    content = dockerfile.read_text()
    assert f"# agent-name: {tmp_path.name.lower()}" in content
    assert "FROM claude-agent" in content


def test_create_refuses_if_exists(
    tmp_path: Path,
    svc: CreateService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Dockerfile.agent").write_text("FROM claude-agent\n")
    rc = svc.create()
    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_create_empty_sanitized_name_returns_error(
    tmp_path: Path,
    svc: CreateService,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dir_with_bad_name = tmp_path / "---"
    dir_with_bad_name.mkdir()
    monkeypatch.chdir(dir_with_bad_name)
    rc = svc.create()
    assert rc == 1
    assert "could not derive agent-name" in capsys.readouterr().err
