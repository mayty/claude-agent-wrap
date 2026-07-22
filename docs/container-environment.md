<!-- This file has been created with the assistance of an AI tool. -->
# Container Environment Variables

These vars are set by the wrapper on every `docker run`, regardless of provider (not baked into the image, so overriding them doesn't require a rebuild):

## Always-injected vars

| Var | Value |
| --- | --- |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |
| `AGENT_INSTANCE_ID` | `<agent-name>-<uuid>` — also the value of the `agent-wrap.instance-id` Docker label; the container itself is named `claude-agent-<agent-name>-<uuid>` |
| `AGENT_NAME` | from `# agent-name:` or sanitized project dir |
| `HOME` | `/home/<agent-user>` (default `/home/ubuntu`) |
| `TERM`, `COLORTERM` | forwarded from host shell, defaulting to `xterm-256color` / `truecolor` if unset |

## Provider-injected vars

The active provider injects additional vars via its `get_agent_env()`, plus the connectivity flags its sidecar(s) supply to the agent's `docker run`. See the provider's README:

- [litellm-bedrock](../agent_wrap/domain/providers/litellm_bedrock/README.md)
- [litellm-dashscope](../agent_wrap/domain/providers/litellm_dashscope/README.md)
- [litellm-deepseek](../agent_wrap/domain/providers/litellm_deepseek/README.md)

Separately from `get_agent_env()`, the LiteLLM sidecar layer itself (`agent_wrap/domain/sidecars/litellm.py`) appends `ANTHROPIC_CUSTOM_HEADERS` (carrying the `x-agent-wrap-log-prefix` header used for per-project log routing) to the agent container's env, and sets `AGENT_WRAP_PROVIDER` on the **sidecar** container (not the agent) for per-sidecar provider routing in the shared request/response log. Neither var is declared by a provider's own `get_agent_env()` — they're injected by the shared sidecar wiring that every LiteLLM-based provider goes through.

When the optional Telegram sidecar is active, it similarly injects `TELEGRAM_SIDECAR_URL` (and `TELEGRAM_SIDECAR_TOKEN`, when available) into the agent container. See [Telegram Notifications](telegram-notifications.md).

## Host-forwarded (conditional)

| Var | When forwarded | Effect |
| --- | --- | --- |
| `ENABLE_PROMPT_CACHING_1H` | Only when set in the host shell — forwarded verbatim (including `0`/empty) so you can both allow and explicitly disallow it. | Opts Claude Code into 1-hour prompt cache TTLs instead of the default 5-minute window, which can lower cost on long-running sessions. |

```sh
# Opt into 1-hour prompt caching
ENABLE_PROMPT_CACHING_1H=1 agent run
```

## WSLg (conditional)

On WSL2+WSLg hosts, `DISPLAY` and `WAYLAND_DISPLAY` are forwarded from the host shell; `XDG_RUNTIME_DIR` is set to `/mnt/wslg/runtime-dir`. The same `/mnt/wslg`-directory check that gates these vars also gates the `wl-paste-shim` mount described in [Volume Mounts](volume-mounts.md) — both fire together. See [Clipboard / WSLg](wslg-clipboard.md).
