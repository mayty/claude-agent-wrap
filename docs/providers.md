<!-- This file has been edited with the assistance of an AI tool. -->
# Providers

The wrapper routes Claude Code through a pluggable provider. Each provider implements the [Provider ABC](../agent_wrap/providers/base.py) — four abstract methods (`ensure`, `release`, `get_run_args`, `get_label_args`) — with no assumption about sidecars, proxies, or network topology.

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
| `litellm-bedrock` | AWS Bedrock via LiteLLM sidecar (default) | [README](../agent_wrap/providers/litellm_bedrock/README.md) |
| `litellm-dashscope` | Alibaba Cloud DashScope via LiteLLM sidecar | [README](../agent_wrap/providers/litellm_dashscope/README.md) |
| `litellm-deepseek` | DeepSeek via LiteLLM sidecar (Anthropic-compatible endpoint) | [README](../agent_wrap/providers/litellm_deepseek/README.md) |

All three built-in providers use the shared LiteLLM sidecar.

## Adding a Provider

Adding a new provider: create a subdirectory under [agent_wrap/providers/](../agent_wrap/providers/) with `__init__.py` and `provider.py` implementing the `Provider` ABC. Providers are auto-discovered — drop in a directory and it shows up without any registry edits. If your provider uses a LiteLLM sidecar, subclass `LiteLLMProvider` from [litellm_common](../agent_wrap/providers/litellm_common/) and also provide a `config.yaml` for the proxy config; non-LiteLLM providers do not need `config.yaml`.

## LiteLLM Sidecar

All built-in providers share a single `agent-wrap-litellm` sidecar container to front the upstream API. Auth, traffic routing, and network topology details are specific to each provider's implementation — see its README:

- [`litellm_bedrock/README.md`](../agent_wrap/providers/litellm_bedrock/README.md) — sidecar lifecycle, auth boundary, networking
- [`litellm_dashscope/README.md`](../agent_wrap/providers/litellm_dashscope/README.md)
- [`litellm_deepseek/README.md`](../agent_wrap/providers/litellm_deepseek/README.md)
