# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.providers."""

import inspect
import re
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from agent_wrap.domain.display.service import DisplayService
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.sidecars.service import SidecarService

if TYPE_CHECKING:
    from agent_wrap.domain.providers.base import Provider

# Docker's container-name grammar, narrowed to the lowercase slug a provider name
# must be (litellm_runtime/callback.py validates the same shape for log routing).
_CONTAINER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _config_text(provider: Provider) -> str:
    """
    Return a provider's checked-in config.yaml with comment lines stripped.

    Resolved from the provider class's own module rather than ``_config_path()``, which
    hangs off ``TOOL_DIR`` and so points into the tmp install root under test.
    """
    config_path = Path(inspect.getfile(type(provider))).parent / "config.yaml"
    lines = config_path.read_text(encoding="utf-8").splitlines()
    return "\n".join(line for line in lines if not line.strip().startswith("#"))


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


def test_discovers_anthropic_sub(svc: ProviderService):
    registry = svc.discover_providers()
    assert "litellm-anthropic-sub" in registry


def test_default_is_bedrock(svc: ProviderService):
    p = svc.get_provider()
    assert p.name == "litellm-bedrock"


def test_explicit_bedrock(svc: ProviderService):
    p = svc.get_provider("litellm-bedrock")
    assert p.name == "litellm-bedrock"


def test_get_provider_instances_are_cached(svc: ProviderService):
    """Repeated lookups for one provider reuse the same instance."""
    assert svc.get_provider("litellm-bedrock") is svc.get_provider("litellm-bedrock")


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


def test_every_provider_config_registers_the_file_logger_callback(svc: ProviderService):
    """
    `callbacks: callback.file_logger_instance` is the whole of what puts a sidecar's
    request logging in place, and `success_callback:` must stay absent — it looks like the
    way to register for successes but routes to a list that passthrough requests skip
    entirely. Both facts live only in YAML, where nothing type-checks them, and getting
    either wrong fails *silently*: every request still succeeds while `messages.jsonl`
    stays empty, so `agent stats` and the logs viewer go quiet with nothing to point at.

    A LiteLLM version bump is the likeliest way for that to happen, which is why this
    walks every discovered provider rather than naming them. See
    litellm_anthropic_sub/README.md, "Request logging is registered in three different
    places", for why the split registration is shaped this way.
    """
    for name in svc.discover_providers():
        text = _config_text(svc.get_provider(name))
        assert "callbacks: callback.file_logger_instance" in text, name
        assert "success_callback:" not in text, name


def test_only_anthropic_sub_disables_nonessential_traffic_opt_out(svc: ProviderService):
    """
    litellm-anthropic-sub is the only provider with a real Anthropic-linked session, so
    it's the only one that needs feature-flag evaluation (and /usage) to keep working.
    """
    for name in svc.discover_providers():
        provider = svc.get_provider(name)
        expected = name != "litellm-anthropic-sub"
        assert provider.disable_nonessential_traffic is expected, name


def test_only_anthropic_sub_opts_out_of_the_logs_viewer(svc: ProviderService):
    """
    The viewer exists to feed the statusline's token/cost segment. litellm-anthropic-sub
    is the only provider whose statusline shows seat consumption instead, so it is the
    only one with nothing to feed.
    """
    for name in svc.discover_providers():
        provider = svc.get_provider(name)
        expected = name != "litellm-anthropic-sub"
        assert provider.autostart_logs_viewer is expected, name
