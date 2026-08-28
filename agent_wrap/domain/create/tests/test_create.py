# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.create.service.CreateService."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.constants import (
    AGENT_ASSETS_DIR,
    AGENT_DOCKERFILE_NAME,
    LEGACY_AGENT_DOCKERFILE_NAME,
)
from agent_wrap.domain.create.service import CreateService
from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def svc() -> CreateService:
    return CreateService(display_service=Mock(spec=DisplayService))


def test_create_writes_dockerfile(
    tmp_path: Path, svc: CreateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    rc = svc.create()
    assert rc == 0
    dockerfile = tmp_path / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME
    assert dockerfile.exists()
    content = dockerfile.read_text()
    assert f"# agent-name: {tmp_path.name.lower()}" in content
    assert "FROM claude-agent" in content


def test_create_creates_assets_directory(
    tmp_path: Path, svc: CreateService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert not (tmp_path / AGENT_ASSETS_DIR).exists()
    assert svc.create() == 0
    assert (tmp_path / AGENT_ASSETS_DIR).is_dir()


def test_create_refuses_if_exists(
    tmp_path: Path,
    svc: CreateService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    dockerfile = tmp_path / AGENT_ASSETS_DIR / AGENT_DOCKERFILE_NAME
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM claude-agent\n")
    rc = svc.create()
    assert rc == 1
    svc._display.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"{dockerfile} already exists"
    )


def test_create_refuses_when_legacy_dockerfile_present(
    tmp_path: Path,
    svc: CreateService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path / LEGACY_AGENT_DOCKERFILE_NAME
    legacy.write_text("# agent-name: x\nFROM claude-agent\n")
    rc = svc.create()
    assert rc == 1
    svc._display.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"{legacy} already exists. Move it to "
        f"{AGENT_ASSETS_DIR}/{AGENT_DOCKERFILE_NAME} instead of scaffolding a new one."
    )
    assert not (tmp_path / AGENT_ASSETS_DIR).exists()


def test_create_empty_sanitized_name_returns_error(
    tmp_path: Path,
    svc: CreateService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dir_with_bad_name = tmp_path / "---"
    dir_with_bad_name.mkdir()
    monkeypatch.chdir(dir_with_bad_name)
    rc = svc.create()
    assert rc == 1
    svc._display.error.assert_called_once_with(  # pyrefly: ignore [missing-attribute]
        f"could not derive agent-name from directory '{dir_with_bad_name}'"
    )
