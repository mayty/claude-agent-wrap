# This file has been created with the assistance of an AI tool.
"""LiteLLM Bedrock provider — routes Claude Code through AWS Bedrock."""

from __future__ import annotations

from typing import ClassVar

from agent_wrap.providers.litellm_common import LiteLLMProvider


class BedrockProvider(LiteLLMProvider):
    name = "litellm-bedrock"
    image: ClassVar[str] = (
        "ghcr.io/berriai/litellm:v1.83.14-stable"
        "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
    )
    master_key_prefix: ClassVar[str] = "sk-aw-"

    def read_secret_key(self, secrets: dict) -> str:
        try:
            key = secrets["ServiceSpecificCredential"]["ServiceCredentialSecret"]
        except (KeyError, TypeError):
            msg = (
                "litellm-sidecar: .ServiceSpecificCredential.ServiceCredentialSecret "
                "missing or empty in ~/claude_keys.json"
            )
            raise SystemExit(msg) from None
        if not key:
            msg = (
                "litellm-sidecar: .ServiceSpecificCredential.ServiceCredentialSecret "
                "missing or empty in ~/claude_keys.json"
            )
            raise SystemExit(msg)
        return key

    def get_sidecar_env(self, secrets: dict) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": secrets.get("_secret_key", ""),
            "AWS_REGION_NAME": "us-east-1",
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "AWS_BEARER_TOKEN_BEDROCK": master_key,
            "ANTHROPIC_BEDROCK_BASE_URL": f"{base_url}/bedrock",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "AWS_REGION": "us-east-1",
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []
