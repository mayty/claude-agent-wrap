# This file has been edited with the assistance of an AI tool.
"""LiteLLM Dashscope provider — routes Claude Code through Alibaba Cloud DashScope."""

from __future__ import annotations

from typing import Any, ClassVar

from agent_wrap.providers.litellm_common import LiteLLMProvider
from agent_wrap.providers.litellm_common.key_approval import MasterKeyApprovalMixin


class DashscopeProvider(MasterKeyApprovalMixin, LiteLLMProvider):
    name = "litellm-dashscope"
    master_key_prefix: ClassVar[str] = "sk-ds-"
    secret_description: ClassVar[str] = "DashScope (Alibaba Cloud Model Studio) API Key"  # noqa: S105

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "DASHSCOPE_API_KEY": secrets.get("_secret_key", ""),
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": "qwen3.7-plus[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen3.7-max[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3.7-plus[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.6-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "qwen3.6-flash",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
            # Disable prompt caching: DashScope's explicit caching mechanism doesn't work well with Claude Code workflow
            # https://www.alibabacloud.com/help/en/model-studio/context-cache
            "DISABLE_PROMPT_CACHING": "1",
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []

    def get_pricing(self) -> dict[str, dict[str, float]]:
        """Return a flat fallback pricing table for legacy stats compatibility."""
        # Using the ≤256K tier as a reasonable default for aggregated totals.
        return {
            "qwen3.7-plus": {"in": 0.40, "out": 1.60, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.08},
            "qwen3.7-max": {"in": 2.50, "out": 7.50, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.50},
            "qwen3.6-flash": {"in": 0.25, "out": 1.50, "cw_5m": 0.0, "cw_1h": 0.0, "cr": 0.05},
        }

    def get_tiered_pricing(self) -> dict[str, Any] | None:
        """Return the tiered pricing table for DashScope models."""
        return {
            "qwen3.7-plus": {
                "tiers": [
                    {
                        "max_in": 256_000,
                        "in": 0.40,
                        "out": 1.60,
                        "cw_5m": 0.0,
                        "cw_1h": 0.0,
                        "cr": 0.08,
                    },
                    {
                        "max_in": 1_000_000,
                        "in": 1.20,
                        "out": 4.80,
                        "cw_5m": 0.0,
                        "cw_1h": 0.0,
                        "cr": 0.24,
                    },
                ]
            },
            "qwen3.7-max": {
                "tiers": [
                    {
                        "max_in": 1_000_000,
                        "in": 2.50,
                        "out": 7.50,
                        "cw_5m": 0.0,
                        "cw_1h": 0.0,
                        "cr": 0.50,
                    },
                ]
            },
            "qwen3.6-flash": {
                "tiers": [
                    {
                        "max_in": 256_000,
                        "in": 0.25,
                        "out": 1.50,
                        "cw_5m": 0.0,
                        "cw_1h": 0.0,
                        "cr": 0.05,
                    },
                    {
                        "max_in": 1_000_000,
                        "in": 1.00,
                        "out": 4.00,
                        "cw_5m": 0.0,
                        "cw_1h": 0.0,
                        "cr": 0.20,
                    },
                ]
            },
        }

    # --- API key auto-approval (once per sidecar lifetime, via lifecycle hooks) ---

    def on_started(self, master_key: str) -> None:
        self._approve_master_key(master_key)

    def on_stopping(self, master_key: str) -> None:
        self._unapprove_master_key(master_key)
