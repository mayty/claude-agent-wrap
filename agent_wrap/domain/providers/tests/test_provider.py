# This file has been created with the assistance of an AI tool.
"""Tests for the Provider sidecar factory in agent_wrap.domain.providers.base."""

from typing import TYPE_CHECKING, Any, override
from unittest.mock import Mock

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers import base
from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.sidecars.service import (
    LiteLLMSidecar,
    SidecarService,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest_mock


class ConcreteTestProvider(Provider):
    """Concrete subclass for testing the abstract Provider factory."""

    name = "litellm-test"
    image = "test-image:latest"
    master_key_prefix = "sk-test-"
    secret_description = "Test API Key"

    def __init__(
        self,
        state_dir: Path | None = None,
        sidecar_service: SidecarService | None = None,
    ) -> None:
        if sidecar_service is None:
            sidecar_service = Mock(spec=SidecarService)
        super().__init__(sidecar_service=sidecar_service, display_service=Mock(spec=DisplayService))
        self._test_state_dir = state_dir

    @override
    def _state_dir(self) -> Path:
        if self._test_state_dir:
            return self._test_state_dir
        return super()._state_dir()

    @override
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {"UPSTREAM_KEY": secrets.get("api_key", "")}

    @override
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {"API_KEY": master_key, "BASE_URL": base_url}


class NoSecretProvider(ConcreteTestProvider):
    """A provider needing no upstream secret (e.g. fronting a local model)."""

    name = "litellm-test-no-secret"
    secret_description = ""


def test_sidecar_returns_a_litellm_sidecar(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    svc = mocker.Mock(spec=SidecarService)
    svc.create_litellm_sidecar.return_value = mocker.Mock(spec=LiteLLMSidecar)
    provider = ConcreteTestProvider(state_dir=tmp_path, sidecar_service=svc)
    assert provider.sidecar() is svc.create_litellm_sidecar.return_value
    svc.create_litellm_sidecar.assert_called_once()


def test_sidecar_config_carries_provider_bits(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    config = p._sidecar_config()
    assert config["image"] == "test-image:latest"
    assert config["provider_name"] == "litellm-test"
    assert config["master_key_prefix"] == "sk-test-"
    # Per-provider container, and a base port the sidecar scans upward from.
    assert config["container_name"] == "agent-wrap-litellm-test"
    assert config["internal_port"] == 48620
    # Timing knobs map from the provider's class attrs (lock-timeout inputs).
    assert config["cold_start_time"] == 120.0
    assert config["short_circuit_time"] == 2.0
    # Paths are resolved by the provider (introspecting the subclass module).
    assert config["config_path"] == p._config_path()
    assert config["log_dir"] == p._log_dir()
    # Hooks are the provider's bound methods.
    assert config["get_agent_env"]("k", "http://x") == {"API_KEY": "k", "BASE_URL": "http://x"}  # pyrefly: ignore [not-callable]


def test_sidecar_config_declares_the_providers_secret(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p._sidecar_config()["required_secrets"] == [("api_key", "Test API Key")]


def test_provider_without_a_secret_description_declares_no_secrets(tmp_path: Path) -> None:
    """A provider fronting an unauthenticated upstream declares nothing to resolve."""
    p = NoSecretProvider(state_dir=tmp_path)
    assert p.required_secrets() == []
    assert p._sidecar_config()["required_secrets"] == []


def test_disable_nonessential_traffic_defaults_true(tmp_path: Path) -> None:
    """Every provider disables non-essential traffic unless it opts out."""
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p.disable_nonessential_traffic is True


def test_autostart_logs_viewer_defaults_true(tmp_path: Path) -> None:
    """Every provider gets the logs viewer unless it explicitly declines it."""
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p.autostart_logs_viewer is True


def test_sidecar_env_reads_secrets_by_declared_name(tmp_path: Path) -> None:
    """The secrets dict is keyed by the names required_secrets() declared."""
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p.get_sidecar_env({"api_key": "upstream-token"}) == {"UPSTREAM_KEY": "upstream-token"}


def test_config_path_sits_beside_the_provider_module(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    assert p._config_path() == tmp_path / "config.yaml"


def test_sidecar_config_wires_lifecycle_hooks(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    """on_started/on_stopping on the config call through to the provider hooks."""
    p = ConcreteTestProvider(state_dir=tmp_path)
    started = mocker.patch.object(p, "on_started", autospec=True)
    stopping = mocker.patch.object(p, "on_stopping", autospec=True)
    config = p._sidecar_config()
    config["on_started"]("sk-test-k")  # pyrefly: ignore [not-callable]
    config["on_stopping"]("sk-test-k")  # pyrefly: ignore [not-callable]
    started.assert_called_once_with("sk-test-k")
    stopping.assert_called_once_with("sk-test-k")


def test_default_hooks_are_noops(tmp_path: Path) -> None:
    p = ConcreteTestProvider(state_dir=tmp_path)
    # Should not raise.
    p.on_started("sk-test-k")
    p.on_stopping("sk-test-k")


def test_log_dir_is_project_independent() -> None:
    """The log dir is the shared tool-dir store, not under the project's .claude."""
    p = ConcreteTestProvider()
    # Read TOOL_DIR off the module so the conftest monkeypatch is honored.
    assert p._log_dir() == base.TOOL_DIR / "litellm-logs"
    assert ".claude" not in p._log_dir().parts


def test_callback_dir_resolves_to_the_real_litellm_runtime_directory(tmp_path: Path) -> None:
    """
    The mounted callback directory must exist and hold the callback module.

    LiteLLMSidecar._start mounts every .py file in this directory only when
    ``callback_dir.is_dir()``, so a stale path silently mounts nothing and disables
    request logging rather than failing. Assert the real path still resolves.
    """
    callback_dir = ConcreteTestProvider(state_dir=tmp_path)._callback_dir()
    assert callback_dir.is_dir(), f"{callback_dir} does not exist"
    assert (callback_dir / "callback.py").is_file()


def test_container_name_is_derived_from_the_provider_name(tmp_path: Path) -> None:
    """One container per provider is what lets two providers run side by side."""
    assert ConcreteTestProvider(state_dir=tmp_path).container_name == "agent-wrap-litellm-test"
    assert NoSecretProvider(state_dir=tmp_path).container_name == (
        "agent-wrap-litellm-test-no-secret"
    )


def test_subclass_may_override_container_name(tmp_path: Path) -> None:
    """A class attribute shadows the base property — the documented escape hatch."""

    class PinnedProvider(ConcreteTestProvider):
        container_name = "my-own-container"

    assert PinnedProvider(state_dir=tmp_path).container_name == "my-own-container"
