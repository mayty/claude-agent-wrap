# This file has been created with the assistance of an AI tool.
"""Shared fixtures for agent_wrap/commands tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def tool_dir(tmp_path: Path) -> Path:
    """Create a temporary tool directory with minimal structure."""
    d = tmp_path / "tool"
    d.mkdir()
    (d / ".claude_config").mkdir()
    (d / ".agent-launches").mkdir()
    return d
