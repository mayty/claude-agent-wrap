# This file has been created with the assistance of an AI tool.
"""Shared fixtures for agent_wrap/commands tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _patch_path_constants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect path constants to tmp_path for test isolation."""
    for mod in (
        "agent_wrap.config",
        "agent_wrap.lib.utils",
        "agent_wrap.lib.grouping",
        "agent_wrap.commands.logs",
        "agent_wrap.commands.update",
        "agent_wrap.commands.rebuild",
        "agent_wrap.commands.stats",
        "agent_wrap.commands.run",
        "agent_wrap.providers.litellm_common.key_approval",
        "agent_wrap.providers.litellm_common.provider",
    ):
        monkeypatch.setattr(f"{mod}.TOOL_DIR", tmp_path, raising=False)
        monkeypatch.setattr(f"{mod}.GLOBAL_CONFIG_DIR", tmp_path / ".claude_config", raising=False)
        monkeypatch.setattr(f"{mod}.OPS_DIR", tmp_path / "ops", raising=False)
        monkeypatch.setattr(
            f"{mod}.AGENT_LAUNCHES_DIR", tmp_path / ".agent-launches", raising=False
        )


@pytest.fixture
def tool_dir(tmp_path: Path) -> Path:
    """Create a temporary tool directory with minimal structure."""
    d = tmp_path / "tool"
    d.mkdir(exist_ok=True)
    (d / ".claude_config").mkdir(exist_ok=True)
    (d / ".agent-launches").mkdir(exist_ok=True)
    return d
