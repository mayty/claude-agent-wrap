# This file has been created with the assistance of an AI tool.
"""
Shared fixtures for all agent_wrap tests.

Placed at the package root so pytest discovers it for every test file
under ``agent_wrap/**/tests/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from agent_wrap.domain.display.service import DisplayService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from unittest.mock import Mock

    from pytest_mock import MockerFixture


@pytest.fixture(autouse=True)
def _patch_path_constants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect path constants to tmp_path for test isolation."""
    for mod in (
        # --- constants ---
        "agent_wrap.constants",
        # --- lib ---
        "agent_wrap.lib.utils",
        # --- domain ---
        "agent_wrap.domain.stats.service",
        "agent_wrap.domain.secrets.service",
        "agent_wrap.domain.config.service",
        "agent_wrap.domain.updates.service",
        "agent_wrap.domain.pricing.service",
        "agent_wrap.domain.logs.io",
        "agent_wrap.domain.logs.daemon",
        "agent_wrap.domain.logs.server",
        "agent_wrap.domain.logs.normalize",
        "agent_wrap.domain.logs.usage_tracker",
        "agent_wrap.domain.stats.scan",
        "agent_wrap.domain.launch.service",
        "agent_wrap.domain.build.service",
        # --- cli ---
        "agent_wrap.cli.logs.run",
        "agent_wrap.cli.update.run",
        "agent_wrap.cli.rebuild.run",
        "agent_wrap.cli.stats.run",
        "agent_wrap.cli.run.run",
        # --- providers ---
        "agent_wrap.domain.providers.key_approval",
        "agent_wrap.domain.providers.litellm_provider",
    ):
        monkeypatch.setattr(f"{mod}.TOOL_DIR", tmp_path, raising=False)
        monkeypatch.setattr(f"{mod}.GLOBAL_CONFIG_DIR", tmp_path, raising=False)
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


@pytest.fixture
def display_mock(mocker: MockerFixture) -> Mock:
    """Return a spec-mocked DisplayService for use in any test."""
    return mocker.Mock(spec=DisplayService)


@pytest.fixture
def write_dockerfile(tmp_path: Path) -> Callable[[str], Path]:
    """Write content to a temporary Dockerfile.agent and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "Dockerfile.agent"
        p.write_text(content)
        return p

    return _write
