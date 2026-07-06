<!-- This file has been edited with the assistance of an AI tool. -->
# LiteLLM Bedrock Provider

Routes Claude Code through AWS Bedrock via a shared LiteLLM sidecar.

## Lifecycle

Sidecar lifecycle is shared across all LiteLLM providers — see [`litellm_common/README.md`](../litellm_common/README.md).

## Configuration

| Item | Value |
| --- | --- |
| Image | `ghcr.io/berriai/litellm:v1.83.14-stable@sha256:c81eb79...` |
| Master key prefix | `sk-aw-` |
| Sidecar region | `us-east-1` (env: `AWS_REGION_NAME`) |
| Agent base URL | `http://agent-wrap-litellm:4000/bedrock` |

## Credentials

Reads `~/claude_keys.json`:

```json
{
  "BedrockBearerToken": "your-aws-bearer-token"
}
```

The Bedrock key goes **only** to the sidecar. Inside the agent, `AWS_BEARER_TOKEN_BEDROCK` is the proxy's auto-generated master key.

## Env vars

Agent container (injected by `get_agent_env`):

- `CLAUDE_CODE_USE_BEDROCK=1`
- `AWS_REGION=us-east-1`
- `AWS_BEARER_TOKEN_BEDROCK` — the sidecar's master key
- `ANTHROPIC_BEDROCK_BASE_URL` — `http://agent-wrap-litellm:4000/bedrock`

Sidecar container (injected by `get_sidecar_env`):

- `AWS_BEARER_TOKEN_BEDROCK` — the user's actual AWS bearer token
- `AWS_REGION_NAME=us-east-1`

## Config

See [`config.yaml`](config.yaml) for the LiteLLM proxy config — a Bedrock wildcard passthrough (`bedrock/*`) with master-key authentication. It also enables the shared request/response JSONL logging callback (`callback.file_logger_instance`).
