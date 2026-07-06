# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.providers."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import SidecarService


@pytest.fixture
def svc() -> ProviderService:
    """Return a ProviderService with a no-op sidecar service."""
    return ProviderService(
        sidecar_service=Mock(spec=SidecarService),
        display_service=Mock(spec=DisplayService),
    )


def test_discovers_bedrock(svc: ProviderService):
    registry = svc.discover_providers()
    assert "litellm-bedrock" in registry


def test_discovers_dashscope(svc: ProviderService):
    registry = svc.discover_providers()
    assert "litellm-dashscope" in registry


def test_default_is_bedrock(svc: ProviderService):
    p = svc.get_provider()
    assert p.name == "litellm-bedrock"


def test_explicit_bedrock(svc: ProviderService):
    p = svc.get_provider("litellm-bedrock")
    assert p.name == "litellm-bedrock"
