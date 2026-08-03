# This file has been created with the assistance of an AI tool.
"""
Shared fixtures for all agent_wrap tests.

Placed at the package root so pytest discovers it for every test file
under ``agent_wrap/**/tests/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.sidecars.service import SidecarService

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pytest_mock import MockerFixture

    from agent_wrap.domain.providers.models import Tier


@pytest.fixture(autouse=True)
def _patch_path_constants(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirect path constants to tmp_path for test isolation."""
    for mod in (
        "agent_wrap.constants",
        "agent_wrap.lib.utils",
        "agent_wrap.domain.stats.service",
        "agent_wrap.domain.secrets.service",
        "agent_wrap.domain.config.service",
        "agent_wrap.domain.updates.service",
        "agent_wrap.domain.pricing.service",
        "agent_wrap.domain.logs.io",
        "agent_wrap.domain.logs.daemon",
        "agent_wrap.domain.logs.service",
        "agent_wrap.domain.logs.server",
        "agent_wrap.domain.logs.normalize",
        "agent_wrap.domain.logs.usage_tracker",
        "agent_wrap.domain.stats.scan",
        "agent_wrap.domain.status.service",
        "agent_wrap.domain.launch.service",
        "agent_wrap.domain.build.service",
        "agent_wrap.cli.logs.run",
        "agent_wrap.cli.update.run",
        "agent_wrap.cli.rebuild.run",
        "agent_wrap.cli.stats.run",
        "agent_wrap.cli.inspect.run",
        "agent_wrap.cli.run.run",
        "agent_wrap.domain.providers.key_approval",
        "agent_wrap.domain.providers.base",
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


class FakeProvider(Provider):
    """
    A concrete ``Provider`` for tests that only care about pricing.

    Built via the ``make_fake_provider`` factory fixture. The sidecar hooks are
    stubs — nothing that uses this drives a real sidecar.
    """

    name = "fake-provider"
    secret_description: ClassVar[str] = "Fake API Key"  # noqa: S105

    def __init__(
        self,
        display_service: DisplayService | Mock | None = None,
        flat: dict[str, dict[str, float]] | None = None,
        tiered: dict[str, list[Tier]] | None = None,
    ) -> None:
        super().__init__(
            sidecar_service=Mock(spec=SidecarService),
            display_service=display_service or Mock(spec=DisplayService),
        )
        self._flat = flat or {}
        self._tiered = tiered

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {"UPSTREAM_KEY": secrets.get("api_key", "")}

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {"API_KEY": master_key, "BASE_URL": base_url}

    def _get_pricing(self) -> dict[str, dict[str, float]]:
        return self._flat

    def _get_tiered_pricing(self) -> dict[str, list[Tier]]:
        if self._tiered is None:
            raise NotImplementedError
        return self._tiered


@pytest.fixture
def make_fake_provider() -> Callable[..., FakeProvider]:
    """Return a factory building a ``FakeProvider`` with a flat or tiered price table."""

    def _make(
        display_service: DisplayService | Mock | None = None,
        flat: dict[str, dict[str, float]] | None = None,
        tiered: dict[str, list[Tier]] | None = None,
    ) -> FakeProvider:
        return FakeProvider(display_service=display_service, flat=flat, tiered=tiered)

    return _make


@pytest.fixture
def write_dockerfile(tmp_path: Path) -> Callable[[str], Path]:
    """Write content to a temporary Dockerfile.agent and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "Dockerfile.agent"
        p.write_text(content)
        return p

    return _write
