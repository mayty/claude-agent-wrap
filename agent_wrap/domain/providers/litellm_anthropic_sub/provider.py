# This file has been created with the assistance of an AI tool.
"""
LiteLLM Anthropic Sub provider — routes Claude Code through Anthropic's own API.

Unlike every other provider here, this one spends a claude.ai subscription rather than
per-token API credits. That inverts the usual design in load-bearing ways:

- **No `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` in `get_agent_env`.** Either replaces
  the OAuth token Claude Code holds from its own claude.ai login and moves billing onto
  API credits. Only `ANTHROPIC_BASE_URL` is set, per Anthropic's documented gateway
  pattern, so Claude Code forwards its own login upstream.

- **`master_key_prefix = "sk-aw-ant-"` must not start with `sk-ant-oat`** — LiteLLM would
  misclassify our generated master key as a subscription OAuth token.

- **The master key travels on `x-litellm-api-key`, never `Authorization`**, which must
  stay free for the OAuth token. See `MASTER_KEY_HEADER`.

- **`ANTHROPIC_BASE_URL` carries a `/anthropic` suffix**, routing to LiteLLM's verbatim
  passthrough rather than its translating `/v1/messages`, whose rewrites strip the two
  markers Anthropic's OAuth gate uses. See `PASSTHROUGH_PREFIX`.

- **`ANTHROPIC_CUSTOM_HEADERS` pins the upstream `Accept-Encoding` to gzip**, because
  LiteLLM's httpx silently fails to decode `br`/`zstd`. See
  `ACCEPT_ENCODING_OVERRIDE_HEADER`.

- **`secret_description` is empty.** The credential is the agent's own claude.ai login,
  not a pasteable string; a description would make a secret mandatory.

- **No `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`.** The upstream *is* Anthropic, so
  pinning them would freeze the model set and break `/model` tier switching.

- **No `MasterKeyApprovalMixin`.** It exists only because Claude Code prompts before
  sending a custom `ANTHROPIC_API_KEY`, which this provider never sends.

- **`disable_nonessential_traffic = False`.**
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` also disables the feature-flag evaluation
  `/usage` depends on, which is how subscription-seat consumption is checked here.
  `LaunchService._build_env_args` substitutes `DISABLE_AUTOUPDATER=1` in its place.

- **`autostart_logs_viewer = False`.** `ops/statusline.py` renders `rate_limit_segment`
  under this provider, so the viewer would maintain a file nothing consults.

- **`compute_cost` returns `0.0`, not `None`.** A subscription has no marginal per-token
  cost. `0.0` reports a truthful *known* zero, where `None` would flag the bucket as
  `cost_unknown` — and it bypasses the pricing tables entirely, so new Anthropic model
  ids need no maintenance here.
"""

from typing import TYPE_CHECKING, Any, ClassVar, override

from agent_wrap.domain.providers.base import Provider
from agent_wrap.domain.providers.litellm_anthropic_sub.constants import (
    ACCEPT_ENCODING_OVERRIDE_HEADER,
    ACCEPT_ENCODING_OVERRIDE_VALUE,
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
    autostart_logs_viewer: ClassVar[bool] = False

    @override
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        return {}

    @override
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        # The PASSTHROUGH_PREFIX suffix is load-bearing — see its docstring in
        # constants.py. Claude Code appends "/v1/messages" to ANTHROPIC_BASE_URL,
        # so this resolves to the verbatim-forwarding /anthropic/v1/messages
        # route rather than LiteLLM's translating /v1/messages one.
        #
        # ANTHROPIC_CUSTOM_HEADERS separates entries on newlines. The sidecar layer
        # appends a third one (x-agent-wrap-log-prefix) to whatever is set here the
        # same way — see sidecars/litellm.py.
        custom_headers = "\n".join(
            (
                f"{MASTER_KEY_HEADER}: {master_key}",
                f"{ACCEPT_ENCODING_OVERRIDE_HEADER}: {ACCEPT_ENCODING_OVERRIDE_VALUE}",
            )
        )
        return {
            "ANTHROPIC_BASE_URL": f"{base_url}{PASSTHROUGH_PREFIX}",
            "ANTHROPIC_CUSTOM_HEADERS": custom_headers,
        }

    @override
    def compute_cost(
        self,
        model: str,
        usage: TokenUsage,
        *,
        hour: int | None,
        weekday: int | None = None,
        refresh_pricing_data: bool = False,
    ) -> float | None:
        """Report a known zero: subscription usage draws on the seat allowance, not per-token billing."""
        return 0.0
