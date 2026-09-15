# This file has been created with the assistance of an AI tool.
"""
Master-key approval mixin for LiteLLM providers.

Claude Code prompts before sending a custom ``ANTHROPIC_API_KEY`` upstream unless
the key's approval id (its last 20 chars) is listed under
``customApiKeyResponses.approved`` in the global ``.claude.json``. Providers whose
sidecar mints a per-lifetime master key (DashScope, DeepSeek) must pre-approve it
so the agent never sees that prompt.

This is wired to the sidecar's ``on_started`` / ``on_stopping`` hooks — so the key
is approved exactly once when the shared sidecar starts and un-approved once when
it stops, rather than per agent. The per-agent toggling it replaced was a
concurrency bug: one agent's exit could un-approve a key another agent was still
using.

Mix into a ``Provider`` subclass and call ``_approve_master_key`` from
``on_started`` and ``_unapprove_master_key`` from ``on_stopping``.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from agent_wrap.constants import GLOBAL_CONFIG_DIR
from agent_wrap.lib.atomic import atomic_write_json
from agent_wrap.lib.jsonio import read_json_object


def _api_key_approval_id(key: str) -> str:
    """Return the identifier Claude Code uses to track key approval (last 20 chars)."""
    return key[-20:]


def _claude_json_path() -> Path:
    return GLOBAL_CONFIG_DIR / ".claude.json"


class MasterKeyApprovalMixin:
    def _load_claude_json(self) -> dict[str, Any] | None:
        """Load .claude.json, returning {} if missing/empty or None on malformed JSON."""
        path = _claude_json_path()
        if not path.exists():
            return {}
        return read_json_object(path)

    def _save_claude_json(self, data: dict[str, Any]) -> None:
        atomic_write_json(_claude_json_path(), data)

    def _approve_master_key(self, key: str) -> None:
        data = self._load_claude_json()
        if data is None:
            return
        approval_id = _api_key_approval_id(key)
        responses = data.setdefault("customApiKeyResponses", {})
        approved = responses.setdefault("approved", [])
        if approval_id not in approved:
            approved.append(approval_id)
            responses.setdefault("rejected", [])
            self._save_claude_json(data)

    def _unapprove_master_key(self, key: str) -> None:
        data = self._load_claude_json()
        if data is None:
            return
        approval_id = _api_key_approval_id(key)
        approved = data.get("customApiKeyResponses", {}).get("approved", [])
        if approval_id in approved:
            approved.remove(approval_id)
            self._save_claude_json(data)
