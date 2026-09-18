# This file has been edited with the assistance of an AI tool.
"""LiteLLM Dashscope provider — routes Claude Code through Alibaba Cloud DashScope."""

from typing import TYPE_CHECKING, ClassVar, override

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.key_approval import MasterKeyApprovalMixin

if TYPE_CHECKING:
    from typing import Any

    from agent_wrap.domain.providers.models import Tier


class DashscopeProvider(MasterKeyApprovalMixin, Provider):
    name = "litellm-dashscope"
    master_key_prefix: ClassVar[str] = "sk-ds-"
    secret_description: ClassVar[str] = "DashScope (Alibaba Cloud Model Studio) API Key"  # noqa: S105

    @override
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {
            "DASHSCOPE_API_KEY": secrets.get("api_key", ""),
        }

    @override
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": "qwen3.7-plus[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "qwen3.7-max[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "qwen3.7-plus[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "qwen3.6-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "qwen3.6-flash",
            # Explore's built-in "inherit" guard caps it at Opus whenever the session model's name
            # lacks a haiku/sonnet/opus token, true for every non-Anthropic ID. Disable it so
            # Explore tracks the session's running model, not CLAUDE_CODE_SUBAGENT_MODEL.
            "CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
            # Disable prompt caching: DashScope's explicit caching mechanism doesn't work well with Claude Code workflow
            # https://www.alibabacloud.com/help/en/model-studio/context-cache
            "DISABLE_PROMPT_CACHING": "1",
        }

    @override
    def _get_tiered_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, list[Tier]]:
        return {
            "qwen3.7-plus": [
                {
                    "max_in": 256_000,
                    "in_": 0.40,
                    "out": 1.60,
                    "cw_5m": 0.0,
                    "cw_1h": 0.0,
                    "cr": 0.08,
                },
                {
                    "max_in": 1_000_000,
                    "in_": 1.20,
                    "out": 4.80,
                    "cw_5m": 0.0,
                    "cw_1h": 0.0,
                    "cr": 0.24,
                },
            ],
            "qwen3.7-max": [
                {
                    "max_in": 1_000_000,
                    "in_": 2.50,
                    "out": 7.50,
                    "cw_5m": 0.0,
                    "cw_1h": 0.0,
                    "cr": 0.50,
                },
            ],
            "qwen3.6-flash": [
                {
                    "max_in": 256_000,
                    "in_": 0.25,
                    "out": 1.50,
                    "cw_5m": 0.0,
                    "cw_1h": 0.0,
                    "cr": 0.05,
                },
                {
                    "max_in": 1_000_000,
                    "in_": 1.00,
                    "out": 4.00,
                    "cw_5m": 0.0,
                    "cw_1h": 0.0,
                    "cr": 0.20,
                },
            ],
        }

    @override
    def on_started(self, master_key: str) -> None:
        self._approve_master_key(master_key)

    @override
    def on_stopping(self, master_key: str) -> None:
        self._unapprove_master_key(master_key)
