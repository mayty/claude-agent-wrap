# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.providers."""

from __future__ import annotations

import re
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import SidecarService

# Docker's container-name grammar, narrowed to the lowercase slug a provider name
# must be (litellm_runtime/callback.py validates the same shape for log routing).
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


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


def test_discovers_deepseek(svc: ProviderService):
    registry = svc.discover_providers()
    assert "litellm-deepseek" in registry


def test_every_provider_has_a_distinct_container_name(svc: ProviderService):
    """
    Two providers sharing a container name would silently route one's traffic at the
    other's upstream, and would collapse their refcounts into one.
    """
    names = [svc.get_provider(name).container_name for name in svc.discover_providers()]
    assert len(set(names)) == len(names)
    assert all(_CONTAINER_NAME_RE.match(name) for name in names)
