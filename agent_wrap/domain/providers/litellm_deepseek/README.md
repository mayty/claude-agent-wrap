<!-- This file has been edited with the assistance of an AI tool. -->
# LiteLLM DeepSeek Provider

Routes Claude Code through DeepSeek via its own LiteLLM sidecar.

## Lifecycle

The sidecar lifecycle is common to all LiteLLM providers — see [`providers/README.md`](../README.md). DeepSeek adds master-key auto-approval in `.claude.json` so Claude Code never prompts to accept the proxy key.

## Configuration

| Item | Value |
| --- | --- |
| Image | `ghcr.io/berriai/litellm:v1.96.2@sha256:154e23bb...` |
| Master key prefix | `sk-ds-` |
| Sidecar container | `agent-wrap-litellm-deepseek` |
| Agent base URL | `http://agent-wrap-litellm-deepseek:<port>` |
| Upstream endpoint | `https://api.deepseek.com/anthropic` |

`<port>` is resolved when the sidecar starts (scanning upward from 48620) and recorded in the container as `AGENT_WRAP_SIDECAR_PORT`, so several providers' sidecars can run at once — see [`providers/README.md`](../README.md#sidecar-lifecycle).

## Credentials

The primary flow is the interactive prompt on the first `agent run` — this secret is required, so a TTY triggers a prompt when it's missing, and the value is stored encrypted in the secrets storage. Use `agent secrets set litellm-deepseek` / `check` / `clear` to manages it explicitly.

## Env vars

Agent container (injected by `get_agent_env`):

- `ANTHROPIC_API_KEY` — the sidecar's master key
- `ANTHROPIC_BASE_URL` — `http://agent-wrap-litellm-deepseek:<port>`
- `ANTHROPIC_MODEL` / `ANTHROPIC_DEFAULT_OPUS_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` — `deepseek-v4-pro[1m]`
- `ANTHROPIC_DEFAULT_HAIKU_MODEL` — `deepseek-v4-flash[1m]`
- `CLAUDE_CODE_SUBAGENT_MODEL` — `deepseek-v4-flash[1m]`
- `CLAUDE_CODE_EFFORT_LEVEL` — `max`

Sidecar container (injected by `get_sidecar_env`):

- `DEEPSEEK_API_KEY` — the user's actual DeepSeek API key

## Config

See [`config.yaml`](config.yaml) for the LiteLLM proxy config — model-pattern passthrough with DeepSeek API base and key.

## Pricing

Pricing used by `agent stats` is **live-scraped** from DeepSeek's public pricing page, cached for 7 days. If the scrape fails (page unreachable, or DeepSeek changes the page's markup), it silently falls back to the stale cache, or to unknown/`$0` cost if no cache exists yet.
