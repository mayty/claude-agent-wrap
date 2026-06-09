<!-- This file has been edited with the assistance of an AI tool. -->
# LiteLLM DashScope Provider

Routes Claude Code through Alibaba Cloud DashScope via a shared LiteLLM sidecar.

## Lifecycle

Sidecar lifecycle is shared across all LiteLLM providers — see [`litellm_common/README.md`](../litellm_common/README.md). DashScope adds master-key auto-approval in `.claude.json` so Claude Code never prompts to accept the proxy key.

## Configuration

| Item | Value |
| --- | --- |
| Image | `ghcr.io/berriai/litellm:v1.83.14-stable@sha256:c81eb79...` |
| Master key prefix | `sk-ds-` |
| Agent base URL | `http://agent-wrap-litellm:4000` |
| Upstream endpoint | `https://dashscope-intl.aliyuncs.com/apps/anthropic/v1/messages` |

## Credentials

Reads `~/claude_keys.json`:

```json
{
  "DashScopeAPIKey": "your-dashscope-api-key"
}
```

## Env vars

Agent container (injected by `get_agent_env`):

- `ANTHROPIC_API_KEY` — the sidecar's master key
- `ANTHROPIC_BASE_URL` — `http://agent-wrap-litellm:4000`
- `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` — `qwen3.7-plus[1m]`
- `ANTHROPIC_DEFAULT_OPUS_MODEL` — `qwen3.7-max[1m]`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` — `qwen3.6-flash`
- `CLAUDE_CODE_SUBAGENT_MODEL` — `qwen3.6-flash`
- `CLAUDE_CODE_EFFORT_LEVEL` — `max`
- `DISABLE_PROMPT_CACHING` — `1`

Sidecar container (injected by `get_sidecar_env`):

- `DASHSCOPE_API_KEY` — the user's actual DashScope API key

## Config

See [`config.yaml`](config.yaml) for the LiteLLM proxy config — model-pattern routes with DashScope API base and key.
