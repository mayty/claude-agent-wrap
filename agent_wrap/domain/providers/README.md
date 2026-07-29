<!-- This file has been edited with the assistance of an AI tool. -->

# Providers

The provider plugin system. Each provider lives in its own subdirectory with a `provider.py`, a `config.yaml`, and a `README.md`; they are auto-discovered, so adding one needs no registry edit. Every provider is LiteLLM-backed, so the `Provider` ABC in [base.py](base.py) is the only base class to subclass — there is no separate LiteLLM base.

Shared across all of them: the logging callback mounted into the sidecar (`litellm_runtime/`) and the log-record types the `logs` domain reads ([models.py](models.py)).

## Sidecar lifecycle

The lifecycle is built for massive concurrency (hundreds of parallel `agent run`
jobs sharing one sidecar). The runner holds **one shared lock** around the whole
launch and consults **one common `SidecarTracker`**; individual sidecars are pure
container mechanics. Three rules make it safe:

1. **The image pull happens lock-free, in `prepare()`, *before* the lock** — a cold
   pull (minutes) must never block the herd inside the lock.
2. **One runner-held lock wraps the entire ensure-all phase** (start decision + health
   poll for every sidecar), so the whole launch is atomic against concurrent
   launchers. Its timeout is `Σ(cold_start_time + X·short_circuit_time)` where `X` =
   `EXPECTED_QUEUE_DEPTH` (overridable via `AGENT_EXPECTED_QUEUE_DEPTH`): one cold
   start plus the whole queue draining the hot path — so a herd never trips the
   deadline. `cold_start_time` ≈ a cold start; `short_circuit_time` ≈ one agent's
   hot-path walk.
3. **Starts have priority; stops yield.** A starting run holds a lock-file ticket
   while it waits for the shared lock (bounded by the timeout above); a stopping run
   blocks for the lock indefinitely but always yields it back while any start ticket
   is still held. So a stop can never race a launch (see below).

- **Lazy start**: first `agent` launch creates the `agent-wrap-net` bridge and starts the sidecar, waiting for the Docker healthcheck (up to 90 s) — all inside the shared lock, on success only.
- **Lock-file registries**: instead of a heartbeat, the tracker keeps two directories of per-run files (named by `instance_id`) under `<tool_dir>/.agent-launches/`, each **held under an exclusive `flock` for as long as its owner lives**. `start-waiters/` holds a ticket from just before a run contends for the shared lock until it acquires it (the priority signal); `running/` holds a registration from the last action under the lock (just before the agent launches) until the run exits. Liveness is tested by **lockability, never by PID**: a file whose lock can be taken has lost its owner (the kernel drops `flock`s on process death), so it is reaped as stale; a file that can't be locked has a live owner. This is immune to PID recycling and self-cleans after a crash — no heartbeat timestamp, no `docker ps` count, no grace window.
- **Release-based stop**: on exit the runner first drops its own `running/` registration, then takes the shared lock *blocking*. While any `start-waiters/` ticket is still held it releases the lock, waits briefly, and retries — starts keep priority. Once no starter is waiting, it stops the sidecars only if no *other* run's `running/` registration is still held (no agent live anywhere). The teardown runs even when `ensure()` fails, so it is the single home for the stop decision; all sidecars stop together, in reverse order.
- **Master key**: minted in memory on first start, passed via `-e LITELLM_MASTER_KEY`. Recovered via `docker inspect` on subsequent launches. Never written to disk. Providers that pre-approve the key in `.claude.json` do so once per sidecar lifetime via the `on_started`/`on_stopping` hooks (not per agent).
- **Network attach**: if the agent runs on a project-supplied network (`--network X` in `agent-run-args`), `ensure()` connects the sidecar to that network.
- **Host network mode** (`AGENT_USE_HOST_NETWORK=1`): sidecar launched with `--network host` on cold start. First-launch-wins.
- **Cross-mode reuse**: bridge-mode agent reaching a host-mode sidecar uses `--add-host agent-wrap-litellm:host-gateway`.
- **Shared lock + registries** live under `<tool_dir>/.agent-launches/` (`sidecars.lock`, `start-waiters/`, `running/`) — the existing host-wide, git-ignored launch-state dir.

## Subclass contract

Subclass `Provider` (`agent_wrap/domain/providers/base.py`) and override:

| Method | Purpose |
| --- | --- |
| `image` (class attr) | Pinned container image (tag + digest) |
| `master_key_prefix` (class attr) | Prefix for the auto-generated master key, per provider (e.g. `sk-aw-` for Bedrock, `sk-ds-` for DashScope/DeepSeek) |
| `secret_description` (class attr) | Human-readable description of the upstream credential. Drives `required_secrets()`, which returns `[("api_key", secret_description)]` — leave empty for an upstream needing no secret. |
| `get_sidecar_env(secrets)` | Env vars for the sidecar container. *secrets* is keyed by the names `required_secrets()` declared, so read the upstream key from `secrets["api_key"]`. |
| `get_agent_env(master_key, base_url)` | Env vars for the agent container |
| `on_started(master_key)` / `on_stopping(master_key)` (optional) | Run once when the sidecar starts/stops — e.g. approve/un-approve the master key in `.claude.json`. Default no-op. |

The base class implements `sidecar()`, returning the `LiteLLMSidecar` built from the attributes + hooks above. The container lifecycle lives in `agent_wrap/domain/sidecars/litellm.py` (`LiteLLMSidecar`, configured by an immutable `LiteLLMSidecarConfig` so it holds no provider back-reference). Locking and the start/stop decision are **not** a sidecar concern: the runner holds one shared lock and consults one `SidecarTracker` (`agent_wrap/domain/sidecars/tracker.py`) — the host-wide lock-file registries of starting and running agents that drive the teardown decision.

The runner assembles the sidecar list in `LaunchService._assemble_sidecars` (`agent_wrap/domain/launch/service.py`), which pairs the provider's `sidecar()` with the runner-level Telegram sidecar (independent of the model backend). It runs each sidecar's lock-free `prepare()` (image pull), then registers a `start-waiters/` ticket, takes the shared lock, clears the ticket, `ensure()`s each (splicing the `docker run` flags each returns into the agent's launch command), and registers a `running/` entry as the last action under the lock; on exit it drops that entry, then under a yield-to-starters lock loop `release()`s each in reverse when no other agent is live. `ensure()` returns those flags as a flat `list[str]`; `cold_start_time` / `short_circuit_time` size the shared lock timeout (see above). With multiple sidecars, only one may request the agent's single `--network` — the others must be reachable via `--add-host`.

Providers may also override the optional `_get_pricing()` / `_get_tiered_pricing()` hooks to feed `agent stats`; the tier arithmetic itself lives in `agent_wrap/domain/providers/pricing.py`.

## Request/response logging

`_start()` mounts the shared logging callback and its helpers (`callback.py`, `string_hasher.py`, `helpers.py`) into the sidecar next to the config (`/etc/litellm/` — LiteLLM resolves callback modules relative to the config file's directory) and bind-mounts a host log dir into it. Each provider's `config.yaml` references the callback via `litellm_settings.callbacks: callback.file_logger_instance`.

- **What it does**: appends one JSON line per LLM call (request messages + `proxy_server_request` + full response) to a session-specific log file.
- **Shared mount**: because a single sidecar (first-launch-wins) serves *every* project on the host, the bind-mounted host dir is project-independent: `<tool_dir>/litellm-logs` → `/var/log/agent-wrap`. The callback writes to `/var/log/agent-wrap/<project_hash>/<provider>/<session_id>/messages.jsonl`.
- **Per-request project routing**: `<project_hash>` is the SHA-256 (16 hex chars) of the launching project's resolved path. The wrapper injects it as the `x-agent-wrap-log-prefix` request header via Claude Code's `ANTHROPIC_CUSTOM_HEADERS`; the callback reads it from `proxy_server_request.headers` (falls back to `unknown-project`).
- **Per-sidecar provider routing**: `<provider>` is fixed for the sidecar's lifetime, so it travels as the `AGENT_WRAP_PROVIDER` container env var (set in `_start`) rather than per-request; the callback reads it from `os.environ` (falls back to `unknown-provider`).
- **Session routing**: the `session_id` is extracted from Claude Code's `x-claude-code-session-id` HTTP header (falls back to `unknown-session`).
- **Per-project symlink**: on launch the wrapper points `<project>/.claude/litellm-logs` at `<tool_dir>/litellm-logs/<project_hash>` (see `config.link_litellm_logs`), so the `agent logs` viewer reads the per-project `<provider>/<session>` layout unchanged. A pre-existing real `litellm-logs` directory from the old per-project scheme is moved aside to `litellm-logs-bkp` rather than clobbered.
- **Always on**: this is a proof-of-concept "see what the agent sent upstream" log — no flag, no UI, no DB. Logging failures are swallowed so they never break the proxy.
- **Directory-per-session design**: accommodates future expansion (e.g., separate files for tools, errors, etc. within the same session directory).
- **Caveat**: Bedrock uses a passthrough route; the `x-claude-code-session-id` header already routes correctly through it, so the sibling `x-agent-wrap-log-prefix` header is expected to as well. If records ever land under `unknown-project/`, custom-header forwarding on that route is the thing to check.
