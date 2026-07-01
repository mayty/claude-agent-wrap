# This file has been created with the assistance of an AI tool.
"""Shared fixtures for agent_wrap top-level tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.fixture(autouse=True)
def _patch_path_constants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect path constants to tmp_path for test isolation."""
    for mod in (
        "agent_wrap.config",
        "agent_wrap.lib.utils",
        "agent_wrap.lib.grouping",
        "agent_wrap.providers.litellm_common.key_approval",
        "agent_wrap.providers.litellm_common.provider",
        "agent_wrap.secrets",
    ):
        monkeypatch.setattr(f"{mod}.TOOL_DIR", tmp_path, raising=False)
        monkeypatch.setattr(f"{mod}.GLOBAL_CONFIG_DIR", tmp_path, raising=False)
        monkeypatch.setattr(f"{mod}.OPS_DIR", tmp_path / "ops", raising=False)
        monkeypatch.setattr(
            f"{mod}.AGENT_LAUNCHES_DIR", tmp_path / ".agent-launches", raising=False
        )


@pytest.fixture
def write_dockerfile(tmp_path: Path) -> Callable[[str], Path]:
    """Write content to a temporary Dockerfile.agent and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "Dockerfile.agent"
        p.write_text(content)
        return p

    return _write
