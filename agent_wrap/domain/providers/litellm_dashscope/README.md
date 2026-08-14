<!-- This file has been edited with the assistance of an AI tool. -->
# LiteLLM DashScope Provider

Routes Claude Code through Alibaba Cloud DashScope via its own LiteLLM sidecar.

## Lifecycle

The sidecar lifecycle is common to all LiteLLM providers — see [`providers/README.md`](../README.md). DashScope adds master-key auto-approval in `.claude.json` so Claude Code never prompts to accept the proxy key.

## Configuration

| Item | Value |
| --- | --- |
| Image | `ghcr.io/berriai/litellm:v1.96.2@sha256:154e23bb...` |
| Master key prefix | `sk-ds-` |
| Sidecar container | `agent-wrap-litellm-dashscope` |
| Agent base URL | `http://agent-wrap-litellm-dashscope:<port>` |
| Upstream endpoint | `https://dashscope-intl.aliyuncs.com/apps/anthropic/v1/messages` |

`<port>` is resolved when the sidecar starts (scanning upward from 48620) and recorded in the container as `AGENT_WRAP_SIDECAR_PORT`, so several providers' sidecars can run at once — see [`providers/README.md`](../README.md#sidecar-lifecycle).

## Credentials

The primary flow is the interactive prompt on the first `agent run` — this secret is required, so a TTY triggers a prompt when it's missing, and the value is stored encrypted in the secrets storage. Use `agent secrets set litellm-dashscope` / `check` / `clear` to manages it explicitly.

## Env vars

Agent container (injected by `get_agent_env`):

- `ANTHROPIC_API_KEY` — the sidecar's master key
- `ANTHROPIC_BASE_URL` — `http://agent-wrap-litellm-dashscope:<port>`
- `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` — `qwen3.7-plus[1m]`
- `ANTHROPIC_DEFAULT_OPUS_MODEL` — `qwen3.7-max[1m]`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` — `qwen3.6-flash`
- `CLAUDE_CODE_SUBAGENT_MODEL` — `qwen3.6-flash`
- `CLAUDE_CODE_EFFORT_LEVEL` — `max`
- `DISABLE_PROMPT_CACHING` — `1` — DashScope's explicit [context-cache mechanism](https://www.alibabacloud.com/help/en/model-studio/context-cache) doesn't work well with Claude Code's prompt-caching workflow

Sidecar container (injected by `get_sidecar_env`):

- `DASHSCOPE_API_KEY` — the user's actual DashScope API key

## Config

See [`config.yaml`](config.yaml) for the LiteLLM proxy config — model-pattern routes with DashScope API base and key.

## Pricing

Unlike Bedrock and DeepSeek, DashScope pricing used by `agent stats` is a **hardcoded, static tiered table** in `provider.py` (`_get_tiered_pricing`) — there is no scraper or automatic refresh. If Alibaba changes DashScope pricing or tier breakpoints, this table needs manual updating.
