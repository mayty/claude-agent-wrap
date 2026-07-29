<!-- This file has been edited with the assistance of an AI tool. -->
# Providers

The wrapper routes Claude Code through a pluggable provider. Every provider routes model traffic through a LiteLLM sidecar — the [Provider ABC](../agent_wrap/domain/providers/base.py) declares it, so this is structural, not a convention. A provider is a thin factory: a `sidecar()` method returns the shared proxy container, and the provider supplies the image pin, the agent-side env vars, and its pricing table. The launcher ensures the sidecar before `docker run`, splices the connectivity flags it returns into the agent's launch command, and releases it after the agent exits.

Select a provider via the `AGENT_PROVIDER` environment variable (default: `litellm-bedrock`). An unknown provider name is a hard error — the wrapper exits and lists the available providers:

```sh
source agent-wrap.bashrc
export AGENT_PROVIDER=litellm-deepseek
agent run
```

All LiteLLM-based providers share a single sidecar container (`agent-wrap-litellm`), so all agents on the same host must use the same provider. Switching providers while agents are running will cause in-flight agents to 401 on their next API call.

## Available Providers

| Provider | Description | README |
| --- | --- | --- |
| `litellm-bedrock` | AWS Bedrock via LiteLLM sidecar (default) | [README](../agent_wrap/domain/providers/litellm_bedrock/README.md) |
| `litellm-dashscope` | Alibaba Cloud DashScope via LiteLLM sidecar | [README](../agent_wrap/domain/providers/litellm_dashscope/README.md) |
| `litellm-deepseek` | DeepSeek via LiteLLM sidecar (Anthropic-compatible endpoint) | [README](../agent_wrap/domain/providers/litellm_deepseek/README.md) |

All three built-in providers use the shared LiteLLM sidecar.

## Adding a Provider

Adding a new provider: create a subdirectory under [agent_wrap/domain/providers/](../agent_wrap/domain/providers/) with `__init__.py`, a `provider.py` subclassing the `Provider` ABC from [base.py](../agent_wrap/domain/providers/base.py), and a `config.yaml` for the proxy config. Providers are auto-discovered — drop in a directory and it shows up without any registry edits.

A subclass sets `name`, `secret_description`, and usually `master_key_prefix`, then implements the two env hooks (`get_sidecar_env`, `get_agent_env`). Declare the upstream credentials via `secret_description` — leave it empty for an upstream that needs none. See the [subclass contract](../agent_wrap/domain/providers/README.md#subclass-contract) for the full list.

## LiteLLM Sidecar

All built-in providers share a single `agent-wrap-litellm` sidecar container to front the upstream API. Auth, traffic routing, and network topology details are specific to each provider's implementation — see its README:

- [`litellm_bedrock/README.md`](../agent_wrap/domain/providers/litellm_bedrock/README.md) — sidecar lifecycle, auth boundary, networking
- [`litellm_dashscope/README.md`](../agent_wrap/domain/providers/litellm_dashscope/README.md)
- [`litellm_deepseek/README.md`](../agent_wrap/domain/providers/litellm_deepseek/README.md)
