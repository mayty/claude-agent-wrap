# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap/providers/litellm_common/provider.py (the slim factory)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest_mock

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers.litellm_provider import LiteLLMProvider
from agent_wrap.domain.sidecars.service import (
    LiteLLMSidecar,
    SidecarService,
)


class ConcreteTestProvider(LiteLLMProvider):
    """Concrete subclass for testing the abstract LiteLLMProvider factory."""

    name = "litellm-test"
    image = "test-image:latest"
    master_key_prefix = "sk-test-"

    def __init__(
        self,
        state_dir: Path | None = None,
        sidecar_service: SidecarService | None = None,
    ) -> None:
        if sidecar_service is None:
            sidecar_service = Mock(spec=SidecarService)
        super().__init__(sidecar_service=sidecar_service, display_service=Mock(spec=DisplayService))
        self._test_state_dir = state_dir

    def _state_dir(self) -> Path:
        if self._test_state_dir:
            return self._test_state_dir
        return super()._state_dir()

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {"UPSTREAM_KEY": secrets.get("_secret_key", "")}

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {"API_KEY": master_key, "BASE_URL": base_url}

    def get_sidecar_cmd_args(self) -> list[str]:
        return []


# --- sidecars() factory ---


def test_sidecars_returns_one_litellm_sidecar(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    svc = mocker.Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = mocker.Mock(spec=LiteLLMSidecar)
    provider = ConcreteTestProvider(state_dir=tmp_path, sidecar_service=svc)
    sidecars = provider.sidecars()
    assert len(sidecars) == 1
    svc.create_litellm_sidecar.assert_called_once()


def test_sidecar_config_carries_provider_bits(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    config = p._sidecar_config()
    assert config["image"] == "test-image:latest"
    assert config["provider_name"] == "litellm-test"
    assert config["master_key_prefix"] == "sk-test-"
    # Timing knobs map from the provider's class attrs (lock-timeout inputs).
    assert config["cold_start_time"] == 120.0
    assert config["short_circuit_time"] == 2.0
    # Paths are resolved by the provider (introspecting the subclass module).
    assert config["config_path"] == p._config_path()
    assert config["log_dir"] == p._log_dir()
    # Hooks are the provider's bound methods.
    assert config["get_agent_env"]("k", "http://x") == {"API_KEY": "k", "BASE_URL": "http://x"}  # type: ignore[not-callable]
    # required_secrets defaults to empty when the provider doesn't declare any.
    assert config["required_secrets"] == []


def test_sidecar_config_wires_lifecycle_hooks(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    """on_started/on_stopping on the config call through to the provider hooks."""
    p = ConcreteTestProvider(state_dir=tmp_path)
    started = mocker.patch.object(p, "on_started")
    stopping = mocker.patch.object(p, "on_stopping")
    config = p._sidecar_config()
    config["on_started"]("sk-test-k")  # type: ignore[not-callable]
    config["on_stopping"]("sk-test-k")  # type: ignore[not-callable]
    started.assert_called_once_with("sk-test-k")
    stopping.assert_called_once_with("sk-test-k")


# --- default lifecycle hooks are no-ops ---


def test_default_hooks_are_noops(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    # Should not raise.
    p.on_started("sk-test-k")
    p.on_stopping("sk-test-k")


# --- path resolvers ---


def test_log_dir_is_project_independent() -> None:
    """The log dir is the shared tool-dir store, not under the project's .claude."""
    p = ConcreteTestProvider()
    assert p._log_dir() == p._tool_dir() / "litellm-logs"
    assert ".claude" not in p._log_dir().parts
