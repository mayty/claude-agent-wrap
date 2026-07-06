# This file has been created with the assistance of an AI tool.
"""
Sidecar management domain service.

This is the ONLY public API for the sidecar subpackage. Every other domain
subpackage accesses sidecar functionality through an injected
``SidecarService`` instance — never by importing the internal modules
(``base``, ``litellm``, ``telegram``, ``tracker``) directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_wrap.domain.sidecars.constants import ROLE_LABEL, ROLE_VALUE
from agent_wrap.domain.sidecars.litellm import LiteLLMSidecar
from agent_wrap.domain.sidecars.models import LiteLLMSidecarConfig, TelegramSidecarConfig
from agent_wrap.domain.sidecars.telegram import TelegramSidecar
from agent_wrap.domain.sidecars.tracker import SidecarTracker

if TYPE_CHECKING:
    from pathlib import Path

    from agent_wrap.domain.display.service import DisplayService


class SidecarService:
    """
    Factory and coordinator for sidecar instances.

    Injected via constructor DI into every domain service that needs to
    create or manage sidecars.
    """

    #: Label value used to identify agent containers.
    role_label: str = ROLE_LABEL
    #: Label role value identifying agent containers.
    role_value: str = ROLE_VALUE

    def __init__(self, display_service: DisplayService) -> None:
        self._display = display_service

    # --- Factory methods ---

    def create_tracker(self, tool_dir: Path) -> SidecarTracker:
        """Create a new ``SidecarTracker`` scoped to *tool_dir*."""
        return SidecarTracker(tool_dir)

    def create_telegram_sidecar(self, **kwargs: Any) -> TelegramSidecar:
        """Create a ``TelegramSidecar`` from keyword arguments forwarded to the config."""
        return TelegramSidecar(TelegramSidecarConfig(**kwargs), display_service=self._display)

    def create_litellm_sidecar(self, **kwargs: Any) -> LiteLLMSidecar:
        """Create a ``LiteLLMSidecar`` from keyword arguments forwarded to the config."""
        return LiteLLMSidecar(LiteLLMSidecarConfig(**kwargs), display_service=self._display)

    def telegram_required_secrets(self) -> list[tuple[str, str]]:
        """Return the secrets required by the Telegram sidecar."""
        return TelegramSidecar.required_secrets()
