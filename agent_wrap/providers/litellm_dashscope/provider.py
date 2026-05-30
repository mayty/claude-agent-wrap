# This file has been created with the assistance of an AI tool.
"""LiteLLM Dashscope provider — routes Claude Code through Alibaba Cloud DashScope."""

from __future__ import annotations

from typing import ClassVar

from agent_wrap.providers.litellm_common import LiteLLMProvider


class DashscopeProvider(LiteLLMProvider):
    name = "litellm-dashscope"
    image: ClassVar[str] = (
        "ghcr.io/berriai/litellm:v1.83.14-stable"
        "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
    )
    lock_file: ClassVar[str] = "litellm-dashscope.lock"
    refcount_file: ClassVar[str] = "litellm-dashscope.refcount"
    master_key_prefix: ClassVar[str] = "sk-ds-"

    def read_secret_key(self, secrets: dict) -> str:
        key = secrets.get("DashScopeAPIKey", "")
        if not key:
            raise SystemExit(
                "litellm-sidecar: .DashScopeAPIKey missing or empty in ~/claude_keys.json"
            )
        return key

    def get_sidecar_env(self, secrets: dict) -> dict[str, str]:
        return {
            "DASHSCOPE_API_KEY": secrets.get("_secret_key", ""),
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []
