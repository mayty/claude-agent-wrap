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
