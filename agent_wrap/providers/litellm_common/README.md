<!-- This file has been edited with the assistance of an AI tool. -->

# LiteLLM Common Provider (internal)

The shared base class for all LiteLLM-based providers. Subclass this (not the bare `Provider` ABC) when adding a new LiteLLM-backed provider.

## Sidecar lifecycle

- **Lazy start**: first `agent` launch creates the `agent-wrap-net` bridge and starts the sidecar (under `flock`), waits for Docker healthcheck, up to ~90 s.
- **Refcount**: each running agent registers its `AGENT_INSTANCE_ID` in the provider's `refcount` file. Parallel agents share one sidecar.
- **Refcount-based stop**: last agent exits → sidecar stopped. Stale entries reconciled against `docker ps`.
- **Master key**: minted in memory on first start, passed via `-e LITELLM_MASTER_KEY`. Recovered via `docker inspect` on subsequent launches. Never written to disk.
- **Network attach**: if the agent runs on a project-supplied network (`--network X` in `agent-run-args`), `ensure()` connects the sidecar to that network.
- **Host network mode** (`AGENT_USE_HOST_NETWORK=1`): sidecar launched with `--network host` on cold start. First-launch-wins.
- **Cross-mode reuse**: bridge-mode agent reaching a host-mode sidecar uses `--add-host agent-wrap-litellm:host-gateway`.

## Subclass contract

Subclass `LiteLLMProvider` and override:

| Method | Purpose |
| --- | --- |
| `image` (class attr) | Pinned container image (tag + digest) |
| `master_key_prefix` (class attr) | Prefix for the auto-generated master key (e.g. `sk-aw-`) |
| `read_secret_key(secrets)` | Extract the upstream API key from `~/claude_keys.json` |
| `get_sidecar_env(secrets)` | Env vars for the sidecar container |
| `get_agent_env(master_key, base_url)` | Env vars for the agent container |
| `get_sidecar_cmd_args()` | Extra args for the sidecar `docker run` |

The base class implements `ensure()`, `release()`, `get_run_args()`, and `get_label_args()`.

## Request/response logging

`_start()` mounts a shared logging callback (`callback.py`) into the sidecar next to the config (`/etc/litellm/callback.py` — LiteLLM resolves callback modules relative to the config file's directory) and bind-mounts a host log dir into it. Each provider's `config.yaml` references the callback via `litellm_settings.callbacks: callback.file_logger_instance`.

- **What it does**: appends one JSON line per LLM call (request messages + `proxy_server_request` + full response) to a session-specific log file.
- **Where**: host dir `./.claude/litellm-logs/<provider-name>/<session_id>/messages.jsonl` (e.g., `litellm-bedrock`) → `/var/log/agent-wrap/<session_id>/messages.jsonl` in the sidecar. The provider-specific directory is mounted directly, so the callback only needs to append the `session_id`.
- **Session routing**: the `session_id` is extracted from Claude Code's `x-claude-code-session-id` HTTP header (falls back to `unknown-session`).
- **Always on**: this is a proof-of-concept "see what the agent sent upstream" log — no flag, no UI, no DB. Logging failures are swallowed so they never break the proxy.
- **Directory-per-session design**: accommodates future expansion (e.g., separate files for tools, errors, etc. within the same session directory).
- **Caveat**: Bedrock uses a passthrough route; confirm callbacks fire on passthrough (Step 0 spike) before relying on the captured bodies.
