# This file has been created with the assistance of an AI tool.
"""
LiteLLM Anthropic Sub provider — routes Claude Code through Anthropic's own API.

Unlike every other provider here, this one exists to spend a claude.ai
subscription, not to bill per-token API credits. That inverts the
usual design in a few load-bearing ways:

- **No `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` in `get_agent_env`.** Either
  one replaces the active credential Claude Code already holds from its own
  claude.ai login (an OAuth token, `sk-ant-oat...`) and moves billing off the
  subscription onto API credits instead — exactly what this provider must
  not do. Only `ANTHROPIC_BASE_URL` is set, per Anthropic's documented gateway
  pattern (code.claude.com/docs/en/llm-gateway): Claude Code keeps using its own
  login and forwards it as `Authorization: Bearer sk-ant-oat...` plus
  `anthropic-beta: oauth-2025-04-20` through the gateway.

- **`master_key_prefix = "sk-aw-ant-"` must not start with `sk-ant-oat`** — that
  shape is reserved for subscription OAuth tokens; LiteLLM would misclassify our
  own generated master key as one.

- **The master key travels on `x-litellm-api-key`, never `Authorization`.**
  LiteLLM's proxy accepts the master key on that header and leaves
  `Authorization` free to carry the subscription OAuth token upstream — see
  `MASTER_KEY_HEADER` in `constants.py`.

- **`ANTHROPIC_BASE_URL` carries a `/anthropic` suffix.** It routes traffic to
  LiteLLM's verbatim Anthropic passthrough rather than its translating
  `/v1/messages` endpoint, whose rewrites strip the `claude-code-20250219` beta
  value and the `x-anthropic-billing-header` system block — the two markers
  Anthropic's OAuth gate uses to recognize first-party Claude Code traffic. See
  `PASSTHROUGH_PREFIX` in `constants.py` for the full failure mode.

- **`secret_description` is empty.** The credential is the agent's own claude.ai
  login (via `/login` inside the container), not a pasteable string this
  provider could store or prompt for. A non-empty description would make a
  secret mandatory that this provider has no use for.

- **No `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`.** The upstream *is*
  Anthropic, so Claude Code's own model names pass through the `anthropic/*`
  route verbatim. Pinning them would freeze the model set at authoring time and
  break `/model` tier switching.

- **No `on_started`/`on_stopping`, no `MasterKeyApprovalMixin`.** That mixin
  exists only because Claude Code prompts before sending a custom
  `ANTHROPIC_API_KEY` upstream; since this provider never sends one, there is
  nothing to approve.

- **`disable_nonessential_traffic = False`.** `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  also disables Claude Code's feature-flag evaluation against Anthropic's
  backend, which this provider's users need live — `/usage` is the documented
  way to check subscription-seat consumption here (see "Pricing" in
  `README.md`), and it depends on the same Anthropic-backed session that flag
  would starve. `LaunchService._build_env_args` reads this flag and substitutes
  `DISABLE_AUTOUPDATER=1` in its place, so the CLI baked into the image still
  never tries to self-update.

- **`compute_cost` is overridden directly, always returning `0.0`.** A
  subscription has no marginal per-token cost, so there is no rate table to
  maintain, and no dollar figure here would be truthful. Returning `0.0`
  (rather than `None`) matters because `stats/scan.py` only flags a bucket as
  `cost_unknown` when `compute_cost` returns `None` — `0.0` reports a truthful,
  known zero instead. This also bypasses `_get_pricing`/`_build_pricing_table`/
  `ModelKeyMatcher` entirely, so new Anthropic model ids need no maintenance
  here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.litellm_anthropic_sub.constants import (
    MASTER_KEY_HEADER,
    PASSTHROUGH_PREFIX,
)

if TYPE_CHECKING:
    from agent_wrap.domain.pricing.models import TokenUsage


class AnthropicSubProvider(Provider):
    name = "litellm-anthropic-sub"
    master_key_prefix: ClassVar[str] = "sk-aw-ant-"
    secret_description: ClassVar[str] = ""
    disable_nonessential_traffic: ClassVar[bool] = False

    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:  # noqa: ARG002
        return {}

    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        # The PASSTHROUGH_PREFIX suffix is load-bearing — see its docstring in
        # constants.py. Claude Code appends "/v1/messages" to ANTHROPIC_BASE_URL,
        # so this resolves to the verbatim-forwarding /anthropic/v1/messages
        # route rather than LiteLLM's translating /v1/messages one.
        return {
            "ANTHROPIC_BASE_URL": f"{base_url}{PASSTHROUGH_PREFIX}",
            "ANTHROPIC_CUSTOM_HEADERS": f"{MASTER_KEY_HEADER}: {master_key}",
        }

    def compute_cost(
        self,
        model: str,  # noqa: ARG002
        usage: TokenUsage,  # noqa: ARG002
        *,
        refresh_pricing_data: bool = False,  # noqa: ARG002
    ) -> float | None:
        """Report a known zero: subscription usage draws on the seat allowance, not per-token billing."""
        return 0.0
