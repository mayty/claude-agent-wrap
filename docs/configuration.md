<!-- This file has been created with the assistance of an AI tool. -->
# Configuration

These environment variables affect wrapper behavior, not the container's environment.

## `AGENT_PROVIDER` (model-routing backend)

Selects which provider plugin to use. Each provider lives in `agent_wrap/domain/providers/<name>/provider.py` and implements the [Provider ABC](../agent_wrap/domain/providers/base.py). The default is `litellm-bedrock`, preserving historical behavior.

```sh
# Use the default LiteLLM-Bedrock provider (no var needed)
agent run
```

```sh
# Or pick a different one — the launcher fails fast and lists available
# providers if the directory doesn't exist.
AGENT_PROVIDER=litellm-deepseek agent run
```

Providers are auto-discovered by scanning `agent_wrap/domain/providers/*/provider.py` for concrete `Provider` subclasses (`inspect.getmembers()` + `inspect.isabstract()`) — drop in a directory and it shows up in the error message above without any registry edits. A subdirectory without a `provider.py` is skipped, which is how support directories such as `litellm_runtime/` stay out of the registry.

Providers can be mixed freely across concurrent agents — each one has its own `agent-wrap-<provider>` sidecar, resolves its own port, and is torn down only when the last agent using it exits:

```sh
# shell A                                   # shell B
AGENT_PROVIDER=litellm-bedrock agent run    AGENT_PROVIDER=litellm-deepseek agent run
```

See [Providers](providers.md#running-several-providers-at-once) for the details.

## `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent run` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

The same switch also applies to `agent rebuild`: each `RUN` step in a `docker build` (`apt-get`, `pip install`, package downloads) executes in a temporary container on Docker's default bridge — the same path that breaks in the scenario below — so the build is launched with `docker build --network host` under the identical WSL-only gating.

Use this when you run multiple WSL2 distros that each have their own `dockerd`. All WSL2 distros share a single Linux kernel, so the two daemons fight over the kernel's iptables tables — specifically, the second daemon to start installs Docker's standard ruleset on `iptables-legacy`, which flips the legacy `FORWARD` chain policy from `ACCEPT` to `DROP`. Reply traffic to the first distro's existing containers then gets dropped before it reaches `docker0`. Symptom: parent shell stays online, but containers lose all outbound TCP (DNS UDP still works); recovery requires `wsl --shutdown`. Relaunching the container does not help, because the broken state is upstream of `docker0`.

`--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and the FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces, not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. Make in-container services bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).
- The flag also extends to any provider sidecar — when set on the **cold-start** launch, the sidecar is launched with `--network host` as well. First-launch-wins, **per provider**: subsequent launches on that provider adapt to its running mode rather than restarting it, and each provider's sidecar inherits the mode of whichever launch started it, independently of the others. To switch a running sidecar's mode, stop it and start the next launch with the desired flag value.
- In host mode a sidecar's port is a host port, so ports are resolved at start time (scanning upward from 48620) rather than fixed. Two providers' sidecars therefore coexist without collision; the resolved port is recorded in the container and reused by later launches.

## `AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent run` and `agent rebuild` run a best-effort upstream check on every invocation: a `git fetch --tags` against the wrap-dir's tracking branch, then — if an update is available — a `Update agent-wrap now? [y/N]` prompt. On `y`, the wrapper runs `agent update` and returns without launching the container or rebuilding the image; re-run your original command afterwards (nothing needs re-sourcing — `agent` is an executable on `PATH`). On `n` (or Enter), the original command proceeds unchanged.

What counts as "an update available" depends on the branch. On `master`, the prompt only appears when a **newer tag** has been published upstream, and the update fast-forwards to that tag's commit — untagged commits pushed after the latest tag do not trigger a prompt. On any other branch, the check is commit-based: any upstream commit triggers the prompt and the update fast-forwards to the branch tip.

Set `AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. The check is also auto-skipped on any error path — non-git wrap-dir, detached HEAD, fetch failure, or 10-second fetch timeout — so a flaky or offline network never blocks a launch. On `agent run`, it's likewise skipped whenever Claude Code is invoked headlessly (`-p`/`--print`/`--bare`/`--safe-mode` — the same flags that skip the Telegram sidecar), since the `y/N` prompt would otherwise block on `input()` with no one to answer it.

Only `agent run` and `agent rebuild` perform the check. Every other verb (`stats`, `logs`, `inspect`, `cleanup`, `create`, `secrets`, and `update` itself) does not.

## `AGENT_EXPECTED_QUEUE_DEPTH` (parallel-launch tuning)

Each `agent run` briefly coordinates with any other simultaneous launches while a sidecar is started or torn down. `AGENT_EXPECTED_QUEUE_DEPTH` is the expected number of agents queued behind that coordination at once; it sizes how long a launch waits before treating itself as genuinely stuck rather than merely queued.

The default (128) comfortably covers ordinary use, including dozens of agents launching together — you do not need to set it. It also absorbs the extra wait when a launch herd spans several providers: the coordination is host-wide while each launch's budget is sized from its own sidecars, so one launch can queue behind another provider's cold start as well as its own. Raise it only when a script fans out far more simultaneous `agent run` jobs than that, so the extra agents at the back of the queue don't time out while waiting their turn:

```sh
AGENT_EXPECTED_QUEUE_DEPTH=512 agent run
```

Set it to a positive integer; a non-numeric or non-positive value is ignored and the default applies.

## `AGENT_DAY_START_UTC` (stats day-boundary offset)

`agent stats` buckets usage into calendar days. `AGENT_DAY_START_UTC` sets how many hours past UTC midnight a "day" begins — negative values start a day before UTC midnight. Unset, it defaults to `-<host's local UTC offset in hours>`, so days align with host-local midnight, matching prior behavior.

```sh
# Days start at 04:00 UTC instead of local midnight.
AGENT_DAY_START_UTC=4 agent stats
```

Unlike `AGENT_EXPECTED_QUEUE_DEPTH`, a malformed value here is a hard error rather than a silent fallback — an unnoticed typo would otherwise corrupt every day bucket. It must parse as an integer and satisfy `-24 < value < 24`; anything else raises at startup.

## `AGENT_TIMEZONE` (display timezone)

Names an IANA zone (e.g. `Europe/Warsaw`, `America/New_York`) used wherever the wrapper would otherwise fall back to ambient system time:

- **Stats day boundary.** When `AGENT_DAY_START_UTC` is unset, `DAY_START_HOURS` defaults to `AGENT_TIMEZONE`'s current UTC offset instead of the host's local offset. `AGENT_DAY_START_UTC` still wins when both are set — it's a more specific override.
- **Statusline reset time.** The `litellm-anthropic-sub` statusline's "resets at HH:MM" is shown in `AGENT_TIMEZONE` instead of the agent container's own local time (typically UTC, since nothing sets `TZ` in the image). Forwarded into the container the same way as [`ENABLE_PROMPT_CACHING_1H`](container-environment.md#host-forwarded-conditional) — only when set on the host.

```sh
AGENT_TIMEZONE=Europe/Warsaw agent run
```

An unknown zone name is a hard error on the host-side day-boundary path, same philosophy as `AGENT_DAY_START_UTC` above — it raises at startup rather than silently falling back. The in-container statusline, which must never crash the prompt, instead ignores an unknown zone name and falls back to the container's local time.

The [logs viewer](shell-commands.md#agent-logs) is unaffected — it renders timestamps in the browser's own local timezone.

## `AGENT_LOG_DEBUG` (verbose logs-viewer daemon logging)

Setting `AGENT_LOG_DEBUG=1` (or any non-empty value other than `0`/`false`/`no`) enables verbose per-tick/per-step logging in the `agent logs` background viewer daemon. Unset, only always-visible lines print (including a "completed in Ns" line that always prints once an operation's elapsed time exceeds its threshold, even without this flag).

```sh
AGENT_LOG_DEBUG=1 agent logs
```
