<!-- This file has been edited with the assistance of an AI tool. -->
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
- If the project Dockerfile already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).
- The flag also extends to any provider sidecar — when set on the **cold-start** launch, the sidecar is launched with `--network host` as well. First-launch-wins, **per provider**: subsequent launches on that provider adapt to its running mode rather than restarting it, and each provider's sidecar inherits the mode of whichever launch started it, independently of the others. To switch a running sidecar's mode, stop it and start the next launch with the desired flag value.
- In host mode a sidecar's port is a host port, so ports are resolved at start time (scanning upward from 48620) rather than fixed. Two providers' sidecars therefore coexist without collision; the resolved port is recorded in the container and reused by later launches.

## `AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent run` and `agent rebuild` run a best-effort upstream check on every invocation: a `git fetch --tags` against the wrap-dir's tracking branch, then — if an update is available — a `Update agent-wrap now? [y/N]` prompt. On `y`, the wrapper runs `agent update` and returns without launching the container or rebuilding the image; re-run your original command afterwards (nothing needs re-sourcing — `agent` is an executable on `PATH`). On `n` (or Enter), the original command proceeds unchanged.

What counts as "an update available" depends on the branch. On `master`, the prompt only appears when a **newer tag** has been published upstream, and the update fast-forwards to that tag's commit — untagged commits pushed after the latest tag do not trigger a prompt. On any other branch, the check is commit-based: any upstream commit triggers the prompt and the update fast-forwards to the branch tip.

Set `AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. The variable is read before anything else, so setting it means `agent run` and `agent rebuild` neither prompt nor look at Docker — including the running-container check that would otherwise make them exit `1` (see [`agent update`](shell-commands.md#agent-update)). It is not a way to update past that check; it only stops the check from happening. The check is also auto-skipped on any error path — non-git wrap-dir, detached HEAD, fetch failure, or 10-second fetch timeout — so a flaky or offline network never blocks a launch. On `agent run`, it's likewise skipped whenever Claude Code is invoked headlessly (`-p`/`--print`/`--bare`/`--safe-mode` — the same flags that skip the Telegram sidecar), since the `y/N` prompt would otherwise block on `input()` with no one to answer it.

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

## `AGENT_AUTOSTART_LOGS` (logs-viewer autostart opt-out)

`agent run` starts the [`agent logs`](shell-commands.md#agent-logs) background viewer for you, because that viewer is the only thing that keeps the statusline's `Today: ↑… ↓… | $…` segment up to date — without it the statusline reads `run \`agent logs\` for stats` instead. It is started as the very first step of a launch, so its initial walk of the log tree happens while the image is resolved and the LiteLLM sidecar comes up rather than after, and it is **not** stopped when the agent exits: the viewer is a host-level singleton shared by every project, and [`agent logs --stop`](shell-commands.md#agent-logs) is how you stop it by hand. The one thing that stops it for you is [`agent update`](shell-commands.md#agent-update), before it merges; the next launch starts it again. A viewer that is already running or already starting is adopted rather than duplicated, and a failure to start one is a warning that does not block the launch.

The autostart is on by default. Set `AGENT_AUTOSTART_LOGS=0` (or `false`/`no`) to turn it off; exporting the variable with an empty value counts as leaving it unset.

```sh
AGENT_AUTOSTART_LOGS=0 agent run
```

Two launches skip the autostart regardless of this variable. A headless `agent run` (`-p`/`--print`/`--bare`/`--safe-mode`) renders no statusline, so there is no segment to feed; and under the [`litellm-anthropic-sub`](providers.md) provider the statusline shows subscription rate limits instead of token totals, so the file the viewer maintains has no reader. `agent logs` still starts the viewer on demand in both cases.

[`agent inspect`](shell-commands.md#agent-inspect) reports the result as a `logs viewer autostart` row, directly under the viewer's own state: `on`, `OFF (AGENT_AUTOSTART_LOGS)` when you turned it off, or `OFF (<provider> does not use it)` when the provider is what declines it. Setting the variable to `1` under a provider that declines reads as `requested but IGNORED`, flagged in yellow — that combination does nothing, and a plain `off` would leave you guessing which of the two decided it. The report cannot account for headless launches, since that depends on one launch's arguments.

## `AGENT_SPELLCHECK` (prompt spell checking)

Claude Code can underline misspelled words in the prompt input as you type, but it ships no spell checker of its own — it drives an external one. The wrapper supplies that: `hunspell` and the configured dictionaries are installed in the base image, and a `spellcheck` block is injected into the wrapper-global `<wrap-dir>/.claude_config/.claude/settings.json` on launch (see [Injected settings](container-environment.md#injected-settings-not-env-vars)).

Spell checking is **on by default**. Set `AGENT_SPELLCHECK=0` (or `false`/`no`) to turn it off:

```sh
AGENT_SPELLCHECK=0 agent run
```

The env var is an override, not a seed. Unset, whatever the settings file holds wins — so an `"enabled": false` you edited in by hand survives every later launch. Set explicitly, it rewrites `spellcheck.enabled` on every launch, which is what keeps `AGENT_SPELLCHECK=0` working after a previous launch has already written the block. An empty value (`AGENT_SPELLCHECK=`) counts as unset.

The block must live in this tier: Claude Code reads `spellcheck` from user, flag and managed settings only, and ignores it outright in a project's `.claude/settings.json` or `.claude/settings.local.json`.

## `AGENT_SPELLCHECK_LANG` (dictionaries)

A comma-separated list of hunspell dictionaries, default `en_US,ru_RU`. hunspell loads them all and accepts a word found in **any** of them, which is what lets a prompt mix English and Russian without every word of one language being underlined.

```sh
# One rebuild to install the dictionaries, then every launch uses them.
AGENT_SPELLCHECK_LANG=en_GB,de_DE agent rebuild --full
AGENT_SPELLCHECK_LANG=en_GB,de_DE agent run
```

The same value does two jobs, and that is deliberate:

- **At build time** it is passed to `ops/Dockerfile` as the `SPELLCHECK_LANG` build arg, which installs one dictionary package per entry. Changing the list therefore needs an `agent rebuild --full`.
- **At launch time** it is written into `spellcheck.language`, with the same override-vs-seed semantics as `AGENT_SPELLCHECK` above.

Deriving both from one variable is what keeps them in sync. A `language` naming a dictionary that was never installed makes hunspell fail to start, and spell checking then stays off for the whole session with only a debug-log line to say why.

Package names are inconsistent upstream, so the build tries `hunspell-<lang>-<region>` first and falls back to `hunspell-<lang>`: `en_US` → `hunspell-en-us`, `de_DE` → `hunspell-de-de`, but `ru_RU` → `hunspell-ru` and `fr_FR` → `hunspell-fr`. If neither package exists the build fails loudly rather than producing an image where spell checking is silently dead.

Entries are validated host-side against `^[A-Za-z]{2,3}(_[A-Za-z]{2,})?$`, and the joined list against Claude Code's 64-character cap. A malformed value raises at startup rather than falling back to the default — same philosophy as `AGENT_DAY_START_UTC` above, and for the same reason: a silent fallback would install one set of dictionaries and configure another.

One trade-off comes with loading several dictionaries: a typo that happens to be a valid word in one of the other languages is not flagged.
