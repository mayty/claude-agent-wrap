# LiteLLM DashScope Provider

Routes Claude Code through Alibaba Cloud DashScope via a shared LiteLLM sidecar.

## Lifecycle

Sidecar startup, refcount, network attach, and shutdown are handled by the shared `LiteLLMProvider` base class ([`litellm_common/README.md`](../litellm_common/README.md)). Key behaviors:

- **Lazy start**: first `agent` launch creates the `agent-wrap-net` bridge and starts the sidecar (under `flock`), waits for Docker healthcheck (~90 s).
- **Refcount**: each running agent registers its `AGENT_INSTANCE_ID`; last exit stops the sidecar.
- **Master key**: minted in memory on first start, recovered via `docker inspect` on subsequent launches. Never written to disk.
- The sidecar auto-approves the master key in `.claude.json` so Claude Code never prompts to accept the proxy key.

## Configuration

| Item | Value |
| --- | --- |
| Image | `ghcr.io/berriai/litellm:v1.83.14-stable@sha256:c81eb79...` |
| Master key prefix | `sk-ds-` |
| Agent base URL | `http://agent-wrap-litellm:4000` (non-Bedrock passthrough) |

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

Sidecar container (injected by `get_sidecar_env`):

- `DASHSCOPE_API_KEY` — the user's actual DashScope API key

## Model mapping

The proxy maps Claude model names to DashScope equivalents:

| Claude model pattern | DashScope target |
| --- | --- |
| `*opus*` | `anthropic/qwen3.7-max` |
| `*sonnet*` | `anthropic/qwen3.6-plus` |
| `*haiku*` / fallback | `anthropic/qwen3.6-flash` |

## Config

See [`config.yaml`](config.yaml) for the LiteLLM proxy config — model-pattern routes with DashScope API base and key.
