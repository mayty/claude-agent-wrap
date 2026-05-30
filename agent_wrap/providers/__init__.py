# This file has been created with the assistance of an AI tool.
"""Provider auto-discovery and selection."""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from pathlib import Path

from .base import Provider

# Import all provider submodules so their classes register.
_PROVIDERS_DIR = Path(__file__).parent


def _discover_providers() -> dict[str, type[Provider]]:
    """Scan provider subdirectories for concrete Provider subclasses."""
    registry: dict[str, type[Provider]] = {}
    for item in _PROVIDERS_DIR.iterdir():
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


def get_provider(name: str | None = None) -> Provider:
    """Resolve and instantiate a provider by name.

    Falls back to the AGENT_PROVIDER env var, then to "litellm-bedrock".
    """
    import os

    resolved = name or os.environ.get("AGENT_PROVIDER", "litellm-bedrock")
    registry = _discover_providers()
    cls = registry.get(resolved)
    if cls is None:
        available = ", ".join(sorted(registry.keys())) or "(none found)"
        raise SystemExit(f"Unknown provider: {resolved}\nAvailable: {available}")
    return cls()
