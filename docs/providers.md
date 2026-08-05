<!-- This file has been edited with the assistance of an AI tool. -->
# Providers

The wrapper routes Claude Code through a pluggable provider. Every provider routes model traffic through a LiteLLM sidecar — the [Provider ABC](../agent_wrap/domain/providers/base.py) declares it, so this is structural, not a convention. A provider is a thin factory: a `sidecar()` method returns its own proxy container, and the provider supplies the image pin, the agent-side env vars, and its pricing table. The launcher ensures the sidecar before `docker run`, splices the connectivity flags it returns into the agent's launch command, and releases it after the last agent on that provider exits.

Select a provider via the `AGENT_PROVIDER` environment variable (default: `litellm-bedrock`). An unknown provider name is a hard error — the wrapper exits and lists the available providers:

```sh
source agent-wrap.bashrc
export AGENT_PROVIDER=litellm-deepseek
agent run
```

## Running Several Providers at Once

Each provider gets its own sidecar container, named `agent-wrap-<provider>`. Agents on different providers therefore run side by side, in any number of shells:

```sh
# shell A
AGENT_PROVIDER=litellm-bedrock agent run     # starts agent-wrap-litellm-bedrock
# shell B
AGENT_PROVIDER=litellm-deepseek agent run    # starts agent-wrap-litellm-deepseek
```

Two rules make this safe:

- **Teardown is per provider.** When an agent exits, the wrapper stops that provider's sidecar only if no other agent is still using it. Agents on other providers are unaffected, and their sidecars are never touched.
- **Ports do not collide.** A sidecar resolves its port at start time, scanning upward from 48620 for a free one, and records it in the container. Later launches on the same provider adopt the recorded port instead of scanning again. This matters only with [`AGENT_USE_HOST_NETWORK`](configuration.md#agent_use_host_network-wsl-workaround), where a container port is a host port; in the default bridge mode each sidecar has its own network namespace.

## Available Providers

| Provider | Description | README |
| --- | --- | --- |
| `litellm-bedrock` | AWS Bedrock via LiteLLM sidecar (default) | [README](../agent_wrap/domain/providers/litellm_bedrock/README.md) |
| `litellm-dashscope` | Alibaba Cloud DashScope via LiteLLM sidecar | [README](../agent_wrap/domain/providers/litellm_dashscope/README.md) |
| `litellm-deepseek` | DeepSeek via LiteLLM sidecar (Anthropic-compatible endpoint) | [README](../agent_wrap/domain/providers/litellm_deepseek/README.md) |

Each built-in provider fronts its upstream with its own LiteLLM sidecar.

## Adding a Provider

Adding a new provider: create a subdirectory under [agent_wrap/domain/providers/](../agent_wrap/domain/providers/) with `__init__.py`, a `provider.py` subclassing the `Provider` ABC from [base.py](../agent_wrap/domain/providers/base.py), and a `config.yaml` for the proxy config. Providers are auto-discovered — drop in a directory and it shows up without any registry edits.

A subclass sets `name`, `secret_description`, and usually `master_key_prefix`, then implements the two env hooks (`get_sidecar_env`, `get_agent_env`). Declare the upstream credentials via `secret_description` — leave it empty for an upstream that needs none. The container name and port need no attention: the name is derived from `name` (which must be a lowercase slug), and the port is resolved at start time. See the [subclass contract](../agent_wrap/domain/providers/README.md#subclass-contract) for the full list.

## LiteLLM Sidecar

Every built-in provider fronts the upstream API with its own `agent-wrap-<provider>` sidecar container. Auth, traffic routing, and network topology details are specific to each provider's implementation — see its README:

- [`litellm_bedrock/README.md`](../agent_wrap/domain/providers/litellm_bedrock/README.md) — sidecar lifecycle, auth boundary, networking
- [`litellm_dashscope/README.md`](../agent_wrap/domain/providers/litellm_dashscope/README.md)
- [`litellm_deepseek/README.md`](../agent_wrap/domain/providers/litellm_deepseek/README.md)
