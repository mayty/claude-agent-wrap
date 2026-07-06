# This file has been created with the assistance of an AI tool.
"""Domain service for provider discovery and resolution."""

from __future__ import annotations

import importlib
import inspect
import os
from typing import TYPE_CHECKING

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.constants import PROVIDERS_DIR
from agent_wrap.exceptions import ProviderNotFoundError

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.sidecars.service import SidecarService


class ProviderService:
    """
    Domain service wrapping provider discovery and resolution.

    Every consumer outside ``agent_wrap.domain.providers`` accesses providers
    through an injected ``ProviderService`` instance — never by importing the
    module-level functions directly.
    """

    def __init__(self, sidecar_service: SidecarService, display_service: DisplayService) -> None:
        self._sidecar_service = sidecar_service
        self._display = display_service

    def discover_providers(self) -> dict[str, type[Provider]]:
        """Scan provider subdirectories for concrete Provider subclasses."""
        registry: dict[str, type[Provider]] = {}
        for item in PROVIDERS_DIR.iterdir():
            if not item.is_dir():
                continue
            provider_mod = item / "provider.py"
            if not provider_mod.exists():
                continue
            mod = importlib.import_module(f".{item.name}.provider", package=__package__)
            for _attr_name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, Provider)
                    and not inspect.isabstract(obj)
                    and hasattr(obj, "name")
                ):
                    registry[obj.name] = obj
        return registry

    def get_provider(self, name: str | None = None) -> Provider:
        """
        Resolve and instantiate a provider by name.

        Falls back to the AGENT_PROVIDER env var, then to "litellm-bedrock".
        """
        resolved = name or os.environ.get("AGENT_PROVIDER", "litellm-bedrock")
        registry = self.discover_providers()
        cls = registry.get(resolved)
        if cls is None:
            available = ", ".join(sorted(registry.keys())) or "(none found)"
            msg = f"Unknown provider: {resolved}\nAvailable: {available}"
            raise ProviderNotFoundError(msg)
        return cls(
            sidecar_service=self._sidecar_service,
            display_service=self._display,
        )
