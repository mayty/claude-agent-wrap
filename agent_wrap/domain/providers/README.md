<!-- This file has been edited with the assistance of an AI tool. -->

# Providers

The provider plugin system. Each provider lives in its own subdirectory with a `provider.py`, a `config.yaml`, and a `README.md`; they are auto-discovered, so adding one needs no registry edit. Every provider is LiteLLM-backed, so the `Provider` ABC in [base.py](base.py) is the only base class to subclass — there is no separate LiteLLM base.

Shared across all of them: the logging callback mounted into the sidecar (`litellm_runtime/`) and the log-record types the `logs` domain reads ([models.py](models.py)).

## Sidecar lifecycle

Each provider owns **its own** sidecar container, `agent-wrap-<provider>`, so agents on
different providers run concurrently. The lifecycle is built for massive concurrency
(hundreds of parallel `agent run` jobs per sidecar). The runner holds **one shared
lock** around the whole launch and consults **one common `SidecarTracker`**, which
refcounts live agents **per container name**; individual sidecars are pure container
mechanics. Four rules make it safe:

1. **The image pull happens lock-free, in `prepare()`, *before* the lock** — a cold
   pull (minutes) must never block the herd inside the lock.
2. **One runner-held lock wraps the entire ensure-all phase** (start decision + health
   poll for every sidecar), so the whole launch is atomic against concurrent
   launchers. Its timeout is `Σ(cold_start_time + X·short_circuit_time)` where `X` =
   `EXPECTED_QUEUE_DEPTH` (overridable via `AGENT_EXPECTED_QUEUE_DEPTH`): one cold
   start plus the whole queue draining the hot path — so a herd never trips the
   deadline. `cold_start_time` ≈ a cold start; `short_circuit_time` ≈ one agent's
   hot-path walk. Because the lock is global while the budget is sized from this
   launch's own sidecars, a herd spanning several providers can also queue behind a
   foreign cold start; the default budget absorbs it several times over.
3. **Starts have priority; stops yield.** A starting run holds a lock-file ticket
   while it waits for the shared lock (bounded by the timeout above); a stopping run
   blocks for the lock indefinitely but always yields it back while any start ticket
   is still held. So a stop can never race a launch (see below).
4. **The lock stays global, deliberately** — it is not partitioned per provider, because
   it also serializes three things that are genuinely shared: the read-modify-write of
   `.claude.json` in the master-key approval hooks, `prepare_global_config`, and the
   port scan below (probe-then-start is not atomic, so two providers scanning at once
   could pick the same port). Only the `running/` registrations are per container.

- **Lazy start**: the first `agent` launch on a provider creates the shared `agent-wrap-net` bridge and starts that provider's sidecar, waiting for the Docker healthcheck (up to 90 s) — all inside the shared lock, on success only. The network is shared by every sidecar; unique container names keep Docker's embedded DNS unambiguous.
- **Lock-file registries**: instead of a heartbeat, the tracker keeps lock-held per-run files under `<tool_dir>/.agent-launches/`, each **held under an exclusive `flock` for as long as its owner lives**. `start-waiters/<instance_id>` holds a ticket from just before a run contends for the shared lock until it acquires it (the priority signal); `running/<container_name>/<instance_id>` holds a registration from the last action under the lock (just before the agent launches) until the run exits. Liveness is tested by **lockability, never by PID**: a file whose lock can be taken has lost its owner (the kernel drops `flock`s on process death), so it is reaped as stale; a file that can't be locked has a live owner. This is immune to PID recycling and self-cleans after a crash — no heartbeat timestamp, no `docker ps` count, no grace window.
- **Release-based stop, per container**: on exit the runner first drops **all** of its `running/` registrations, so a concurrent stopper sees it as gone everywhere at once. It then takes the shared lock *blocking*; while any `start-waiters/` ticket is still held it releases the lock, waits briefly, and retries — starts keep priority. Once no starter is waiting, it walks its sidecars in reverse and stops each one whose container has no *other* run's registration still held. So an agent on one provider stops that provider's sidecar while agents on other providers keep theirs untouched, and the Telegram container — one name across every provider, hence one refcount — survives until the last agent anywhere exits. The teardown runs even when `ensure()` fails, so it is the single home for the stop decision.
- **Master key**: minted in memory on first start, passed via `-e LITELLM_MASTER_KEY`. Recovered via `docker inspect` on subsequent launches. Never written to disk. Providers that pre-approve the key in `.claude.json` do so once per sidecar lifetime via the `on_started`/`on_stopping` hooks (not per agent).
- **Port**: resolved on cold start by scanning upward from the provider's `internal_port` base (48620) for a free TCP port, then recorded in the container as `AGENT_WRAP_SIDECAR_PORT`; later launches recover it from `docker inspect` rather than re-scanning, exactly like the master key. A fixed port would collide in host-network mode, where the container's port *is* a host port. The probe is not atomic with the container start, so a *foreign* process taking the port in between shows up as a failed health poll (with the container's logs streamed) rather than silent misrouting. An absent or unparseable recorded value stops the launch, as a missing master key does: all the providers share one base, so in host-network mode the base is most probably a *different* provider's sidecar. A guess would connect the agent to the wrong upstream instead of failing. Remove such a container (`docker rm -f agent-wrap-<provider>`) to make the next launch do a cold start.
- **Network attach**: if the agent runs on a project-supplied network (`--network X` in `agent-run-args`), `ensure()` connects the sidecar to that network.
- **Host network mode** (`AGENT_USE_HOST_NETWORK=1`): sidecar launched with `--network host` on cold start. First-launch-wins **per provider** — each provider's sidecar inherits the mode of whichever launch started it, independently of the others.
- **Cross-mode reuse**: bridge-mode agent reaching a host-mode sidecar uses `--add-host agent-wrap-<provider>:host-gateway`. Each sidecar emits an `--add-host` for its own name only, so mixed modes across providers cannot conflict.
- **Shared lock + registries** live under `<tool_dir>/.agent-launches/` (`sidecars.lock`, `start-waiters/`, `running/`) — the existing host-wide, git-ignored launch-state dir.

## Subclass contract

Subclass `Provider` (`agent_wrap/domain/providers/base.py`) and override:

| Method | Purpose |
| --- | --- |
| `name` (class attr) | The `AGENT_PROVIDER` value. Must be a lowercase slug (`[a-z0-9-]+`): it becomes the sidecar's container name and the `<provider>` log-path segment, which `litellm_runtime/callback.py` validates. |
| `image` (class attr) | Pinned container image (tag + digest) |
| `master_key_prefix` (class attr) | Prefix for the auto-generated master key, per provider (e.g. `sk-aw-` for Bedrock, `sk-ds-` for DashScope/DeepSeek) |
| `secret_description` (class attr) | Human-readable description of the upstream credential. Drives `required_secrets()`, which returns `[("api_key", secret_description)]` — leave empty for an upstream needing no secret, or when the credential the agent needs is one it already holds itself (e.g. an existing OAuth login) rather than a string this provider could store. |
| `get_sidecar_env(secrets)` | Env vars for the sidecar container. *secrets* is keyed by the names `required_secrets()` declared, so read the upstream key from `secrets["api_key"]`. |
| `get_agent_env(master_key, base_url)` | Env vars for the agent container. *base_url* already carries the container name and the resolved port — never hard-code either. |
| `autostart_logs_viewer` (class attr, default `True`) | Whether `agent run` starts the `agent logs` background viewer for this provider. That viewer maintains the usage totals the bundled statusline reads, so override to `False` only when this provider's statusline segment is fed from somewhere else. |
| `on_started(master_key)` / `on_stopping(master_key)` (optional) | Run once when the sidecar starts/stops — e.g. approve/un-approve the master key in `.claude.json`. Default no-op. |

Two attributes usually need no override: `container_name` defaults to `agent-wrap-<name>` (assign a literal to pin it), and `internal_port` is only the *preferred base* the cold-start scan begins at — providers share one base and do not need distinct values.

The base class implements `sidecar()`, returning the `LiteLLMSidecar` built from the attributes + hooks above. The container lifecycle lives in `agent_wrap/domain/sidecars/litellm.py` (`LiteLLMSidecar`, configured by an immutable `LiteLLMSidecarConfig` so it holds no provider back-reference). Locking and the start/stop decision are **not** a sidecar concern: the runner holds one shared lock and consults one `SidecarTracker` (`agent_wrap/domain/sidecars/tracker.py`) — the host-wide lock-file registries of starting and running agents that drive the teardown decision. The one thing a sidecar contributes to that decision is its `container_name`, the key the registrations are grouped under.

The runner assembles the sidecar list in `LaunchService._assemble_sidecars` (`agent_wrap/domain/launch/service.py`), which pairs the provider's `sidecar()` with the runner-level Telegram sidecar (independent of the model backend). It runs each sidecar's lock-free `prepare()` (image pull), then registers a `start-waiters/` ticket, takes the shared lock, clears the ticket, `ensure()`s each (splicing the `docker run` flags each returns into the agent's launch command), and registers one `running/<container_name>/` entry per sidecar as the last action under the lock — all together, so a half-ensured launch registers nothing. On exit it drops every entry, then under a yield-to-starters lock loop `release()`s each sidecar in reverse whose container has no other live agent. `ensure()` returns those flags as a flat `list[str]`; `cold_start_time` / `short_circuit_time` size the shared lock timeout (see above). With multiple sidecars, only one may request the agent's single `--network` — the others must be reachable via `--add-host`.

Providers may also override the optional `_get_pricing()` / `_get_tiered_pricing()` hooks to feed `agent stats`; the tier arithmetic itself lives in `agent_wrap/domain/providers/pricing.py`.

## Request/response logging

`_start()` mounts the shared logging callback and its helpers (`callback.py`, `string_hasher.py`, `helpers.py`) into the sidecar next to the config (`/etc/litellm/` — LiteLLM resolves callback modules relative to the config file's directory) and bind-mounts a host log dir into it. Each provider's `config.yaml` references the callback via `litellm_settings.callbacks: callback.file_logger_instance`.

- **What it does**: appends one JSON line per LLM call (request messages + `proxy_server_request` + full response) to a session-specific log file.
- **Shared mount**: because each provider's sidecar (first-launch-wins per provider) serves *every* project on the host, the bind-mounted host dir is project-independent: `<tool_dir>/litellm-logs` → `/var/log/agent-wrap`. Every provider's sidecar mounts the same dir; the callback writes to `/var/log/agent-wrap/<project_hash>/<provider>/<session_id>/messages.jsonl`, so the `<provider>` segment is what keeps concurrent sidecars' subtrees disjoint.
- **Per-request project routing**: `<project_hash>` is the SHA-256 (16 hex chars) of the launching project's resolved path. The wrapper injects it as the `x-agent-wrap-log-prefix` request header via Claude Code's `ANTHROPIC_CUSTOM_HEADERS`; the callback reads it from `proxy_server_request.headers` (falls back to `unknown-project`).
- **Per-sidecar provider routing**: `<provider>` is fixed for the sidecar's lifetime by construction — one container per provider — so it travels as the `AGENT_WRAP_PROVIDER` container env var (set in `_start`) rather than per-request; the callback reads it from `os.environ` (falls back to `unknown-provider`).
- **Session routing**: the `session_id` is extracted from Claude Code's `x-claude-code-session-id` HTTP header (falls back to `unknown-session`).
- **Header normalization and redaction**: `json_safe` serializes *any* mapping as an object, not just `dict`. LiteLLM's `/anthropic/*` passthrough route hands the callback a Starlette `Headers` mapping, which previously hit the `str()` fallback and flattened the whole header set into one `"Headers({...})"` blob — costing the `agent logs` viewer the `x-claude-code-agent-id` it splits subagent threads on, and interning a cleartext credential. Values under a `helpers.REDACTED_HEADERS` key (`authorization`, `x-litellm-api-key`, `x-api-key`, `api-key`) are replaced with `<redacted>` at that same boundary, which also covers the copy the router routes repeat inside `body.secret_fields.raw_headers`. Redaction applies as records are written, so logs written before this landed keep whatever their headers held; delete them if that matters.
- **Per-project symlink**: on launch the wrapper points `<project>/.claude/litellm-logs` at `<tool_dir>/litellm-logs/<project_hash>` (see `config.link_litellm_logs`), so the `agent logs` viewer reads the per-project `<provider>/<session>` layout unchanged. A project that has run several providers shows one subtree per provider beneath it. A pre-existing real `litellm-logs` directory from the old per-project scheme is moved aside to `litellm-logs-bkp` rather than clobbered.
- **Always on**: this is a proof-of-concept "see what the agent sent upstream" log — no flag, no UI, no DB. Logging failures are swallowed so they never break the proxy.
- **Directory-per-session design**: accommodates future expansion (e.g., separate files for tools, errors, etc. within the same session directory).
- **Caveat**: Bedrock uses a passthrough route; the `x-claude-code-session-id` header already routes correctly through it, so the sibling `x-agent-wrap-log-prefix` header is expected to as well. If records ever land under `unknown-project/`, custom-header forwarding on that route is the thing to check.
