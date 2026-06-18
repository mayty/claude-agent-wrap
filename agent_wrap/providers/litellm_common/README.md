<!-- This file has been edited with the assistance of an AI tool. -->

# LiteLLM Common Provider (internal)

The shared base class for all LiteLLM-based providers. Subclass this (not the bare `Provider` ABC) when adding a new LiteLLM-backed provider.

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
3. **The run announces once at lock-exit**, and the releaser takes the lock
   non-blocking. So a stop can never race a launch (see below).

- **Lazy start**: first `agent` launch creates the `agent-wrap-net` bridge and starts the sidecar, waiting for the Docker healthcheck (up to 90 s) — all inside the shared lock, on success only.
- **Activity heartbeat**: the runner writes `{timestamp, fingerprint}` to the tracker's `sidecars-activity.json` (under `<tool_dir>/.agent-launches/`) as the last action under the lock — once for the whole run, not per sidecar. The live agent count is read straight from `docker ps` (label `agent-wrap.role=claude-agent`, one common count) — there is no refcount file.
- **Release-based stop**: on exit the runner takes the shared lock *non-blocking* (skips if held — never blocks a concurrent start). It stops the sidecars only when no agents are live **and** either this run was the last to announce a start (`fingerprint == me`, so nothing newer is in flight → stop immediately) or the heartbeat is older than `idle_grace_sec` (tracker constant, default 30 s; the batch has drained). The grace window also covers the gap between `ensure()` returning and the agent container appearing in `docker ps`. The teardown runs even when `ensure()` fails, so it is the single home for the stop decision; all sidecars stop together, in reverse order.
- **Master key**: minted in memory on first start, passed via `-e LITELLM_MASTER_KEY`. Recovered via `docker inspect` on subsequent launches. Never written to disk. Providers that pre-approve the key in `.claude.json` do so once per sidecar lifetime via the `on_started`/`on_stopping` hooks (not per agent).
- **Network attach**: if the agent runs on a project-supplied network (`--network X` in `agent-run-args`), `ensure()` connects the sidecar to that network.
- **Host network mode** (`AGENT_USE_HOST_NETWORK=1`): sidecar launched with `--network host` on cold start. First-launch-wins.
- **Cross-mode reuse**: bridge-mode agent reaching a host-mode sidecar uses `--add-host agent-wrap-litellm:host-gateway`.
- **Idle linger**: in the narrow case where the only agent exits within `idle_grace_sec` of starting on an otherwise idle host, the (healthy) sidecar lingers until the next run reuses it. Stop it explicitly with `docker stop agent-wrap-litellm` if needed.
- **Shared lock + activity files** live under `<tool_dir>/.agent-launches/` (`sidecars.lock`, `sidecars-activity.json`) — the existing host-wide, git-ignored launch-state dir.

## Subclass contract

Subclass `LiteLLMProvider` and override:

| Method | Purpose |
| --- | --- |
| `image` (class attr) | Pinned container image (tag + digest) |
| `master_key_prefix` (class attr) | Prefix for the auto-generated master key, per provider (e.g. `sk-aw-` for Bedrock, `sk-ds-` for DashScope/DeepSeek) |
| `read_secret_key(secrets)` | Extract the upstream API key from `~/claude_keys.json` |
| `get_sidecar_env(secrets)` | Env vars for the sidecar container |
| `get_agent_env(master_key, base_url)` | Env vars for the agent container |
| `get_sidecar_cmd_args()` | Extra args for the sidecar `docker run` |
| `on_started(master_key)` / `on_stopping(master_key)` (optional) | Run once when the sidecar starts/stops — e.g. approve/un-approve the master key in `.claude.json`. Default no-op. |

The base class implements `sidecars()` (declared on the `Provider` ABC), returning one `LiteLLMSidecar` built from the overridden attributes + hooks above. The container lifecycle lives in `litellm_sidecar.py` (`LiteLLMSidecar`, configured by an immutable `LiteLLMSidecarConfig` so it holds no provider back-reference). Locking and the start/stop decision are **not** a sidecar concern: the runner holds one shared lock and consults one `SidecarTracker` (`agent_wrap/sidecars/tracker.py`) — the host-wide heartbeat, live count, and stop decision.

The runner (`commands/run.py`) collects the sidecars via `collect_sidecars(provider)` — the single place a runner-level sidecar (independent of the model backend) would be appended. It runs each sidecar's lock-free `prepare()` (image pull), then under one shared lock `ensure()`s each (splicing the `docker run` flags each returns into the agent's launch command) and announces once; on exit, under one non-blocking lock and a single `should_stop`, it `release()`s each in reverse. `ensure()` returns those flags as a flat `list[str]`; `cold_start_time` / `short_circuit_time` size the shared lock timeout (see above). With multiple sidecars, only one may request the agent's single `--network` — the others must be reachable via `--add-host`.

Providers may also override the optional `get_pricing()` / `get_tiered_pricing()` hooks (defined on the base `Provider` ABC) to feed `agent stats`.

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
