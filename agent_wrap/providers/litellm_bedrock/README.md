# LiteLLM Bedrock Provider

Routes Claude Code through AWS Bedrock via a shared LiteLLM sidecar.

## Lifecycle

Sidecar startup, refcount, network attach, and shutdown are handled by the shared `LiteLLMProvider` base class ([`litellm_common/README.md`](../litellm_common/README.md)). Key behaviors:

- **Lazy start**: first `agent` launch creates the `agent-wrap-net` bridge and starts the sidecar (under `flock`), waits for Docker healthcheck (~90 s).
- **Refcount**: each running agent registers its `AGENT_INSTANCE_ID`; last exit stops the sidecar.
- **Master key**: minted in memory on first start, recovered via `docker inspect` on subsequent launches. Never written to disk.

## Auth boundary

The user's AWS Bedrock bearer token goes **only** to the sidecar container. Inside the agent, `AWS_BEARER_TOKEN_BEDROCK` is the proxy's auto-generated master key, not the AWS token — Claude Code presents it to the sidecar as a Bearer token in place of an AWS SigV4 header.

## Networking

The sidecar lives on a Docker user-defined bridge named `agent-wrap-net` (created on demand). It is not published on a host port — agents reach it directly over Docker networks:

- **Default agent**: joins `agent-wrap-net` and resolves the sidecar by container name (`agent-wrap-litellm`).
- **Custom-network agent** (`Dockerfile.agent` declares `--network myproj` via `# agent-run-args:`): the sidecar is `docker network connect`'d to that network at launch.
- **`AGENT_USE_HOST_NETWORK=1`**: agent and sidecar both run with `--network host`. Mode is decided at cold start and is **first-launch-wins**.
- **Cross-mode reuse**: bridge-mode agent reaching a host-mode sidecar uses `--add-host agent-wrap-litellm:host-gateway`.

Trade-offs: `--network host` removes container network isolation, `EXPOSE` mappings are meaningless (services bind directly on the WSL distro's interfaces), and services should listen on `127.0.0.1` to avoid LAN exposure.

## Configuration

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
  "ServiceSpecificCredential": {
    "ServiceCredentialSecret": "your-aws-bearer-token"
  }
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

See [`config.yaml`](config.yaml) for the LiteLLM proxy config — a Bedrock wildcard passthrough (`bedrock/*`) with master-key authentication. Langfuse callback support is prepared but commented out.
