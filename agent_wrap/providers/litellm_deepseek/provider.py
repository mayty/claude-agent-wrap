# This file has been edited with the assistance of an AI tool.
"""LiteLLM DeepSeek provider — routes Claude Code through DeepSeek provider."""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from agent_wrap.providers.litellm_common import LiteLLMProvider


class DeepSeekProvider(LiteLLMProvider):
    name = "litellm-deepseek"
    image: ClassVar[str] = (
        "ghcr.io/berriai/litellm:v1.83.14-stable"
        "@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"
    )
    master_key_prefix: ClassVar[str] = "sk-ds-"

    def read_secret_key(self, secrets: dict) -> str:
        key = secrets.get("DeepSeekAPIKey", "")
        if not key:
            msg = "litellm-sidecar: .DeepSeekAPIKey missing or empty in ~/claude_keys.json"
            raise SystemExit(msg)
        return key

    def get_sidecar_env(self, secrets: dict) -> dict[str, str]:
        return {
            "DEEPSEEK_API_KEY": secrets.get("_secret_key", ""),
        }

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        return {
            "ANTHROPIC_API_KEY": master_key,
            "ANTHROPIC_BASE_URL": base_url,
            "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
        }

    def get_sidecar_cmd_args(self) -> list[str]:
        return []

    # --- API key auto-approval ---

    def ensure(
        self,
        *,
        use_host_net: bool,
        instance_id: str,
        agent_network: str | None,
    ) -> None:
        super().ensure(
            use_host_net=use_host_net,
            instance_id=instance_id,
            agent_network=agent_network,
        )
        self._approve_master_key(self._master_key)

    def release(self, instance_id: str) -> None:
        super().release(instance_id)
        self._unapprove_master_key(self._master_key)

    @staticmethod
    def _api_key_approval_id(key: str) -> str:
        """Return the identifier Claude Code uses to track key approval (last 20 chars)."""
        return key[-20:]

    def _claude_json_path(self) -> Path:
        """Resolve the global .claude.json file path."""
        # litellm_deepseek/provider.py -> litellm_deepseek/ -> providers/ -> agent_wrap/ -> repo root
        tool_dir = Path(__file__).resolve().parent.parent.parent.parent
        return tool_dir / ".claude_config" / ".claude.json"

    def _load_claude_json(self) -> dict | None:
        """Load .claude.json, returning {} if missing or None on malformed JSON."""
        path = self._claude_json_path()
        if not path.exists():
            return {}
        try:
            text = path.read_text()
            if not text.strip():
                return {}
            return json.loads(text)
        except (json.JSONDecodeError, OSError):
            return None

    def _save_claude_json(self, data: dict) -> None:
        """Atomically write .claude.json."""
        path = self._claude_json_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)

    def _approve_master_key(self, key: str) -> None:
        """Add the current master key's approval ID to .claude.json."""
        data = self._load_claude_json()
        if data is None:
            return
        approval_id = self._api_key_approval_id(key)
        approved = data.setdefault("customApiKeyResponses", {}).setdefault("approved", [])
        if approval_id not in approved:
            approved.append(approval_id)
            data.setdefault("customApiKeyResponses", {})["rejected"] = data.get(
                "customApiKeyResponses", {}
            ).get("rejected", [])
            self._save_claude_json(data)

    def _unapprove_master_key(self, key: str) -> None:
        """Remove the current master key's approval ID from .claude.json."""
        data = self._load_claude_json()
        if data is None:
            return
        approval_id = self._api_key_approval_id(key)
        approved = data.get("customApiKeyResponses", {}).get("approved", [])
        if approval_id in approved:
            approved.remove(approval_id)
            self._save_claude_json(data)
