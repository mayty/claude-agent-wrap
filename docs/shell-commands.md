<!-- This file has been edited with the assistance of an AI tool. -->
# Shell Commands

`agent` is an executable (`bin/agent`) whose first argument is a verb that selects the operation. All verbs forward to `python3 -m agent_wrap` and run on the host. Sourcing `agent-wrap.bashrc` adds `bin/` to your `PATH` (so `agent` resolves) and, under bash only, registers tab-completion for the verbs and their flags. Completion is live, not generated: `complete -F _agent_complete agent` (in `agent-wrap.bashrc`) calls `AGENT_COMPLETE=1 agent <cword> <words...>`, which routes to `_complete()` in `agent_wrap/__main__.py` and calls each command module's own `complete()` function directly — there is no generated completion file and no `make` step to keep it in sync. Programmatic callers that only need to launch `agent` can instead put `<repo>/bin` on `PATH` or symlink `bin/agent` into a directory already on `PATH` — no sourcing required.

| Verb | Purpose |
| --- | --- |
| `run` | Launch Claude Code in a container |
| `rebuild` | Rebuild the project or base image |
| `create` | Scaffold a `Dockerfile.agent` |
| `stats` | Aggregate token usage and cost |
| `logs` | Browse LiteLLM request logs in a local web viewer |
| `cleanup` | Delete leftover logs and registry entries from removed projects |
| `update` | Pull latest wrapper source |
| `secrets` | Manage encrypted sidecar/provider secrets |

## `agent run`

```
agent run [--base] [claude-code-args...]
```

Launches Claude Code in a Docker container against the resolved image for the current directory. Records the project path in `<wrap-dir>/.agent-launches/projects.txt` for use by `agent stats`. Paths are stored in a compressed, grouped form (e.g. sibling directories collapse to `/a/{x,y,z}`, shared prefixes collapse to `{N}/rest`) rather than one plain path per line — read it with `agent stats`/`agent logs`, not by grepping the file directly.

> This command and `agent rebuild` check for wrapper updates on every invocation, except headless `agent run` invocations (`-p`/`--print`/`--bare`/`--safe-mode`). See [`AGENT_SKIP_UPDATE_CHECK`](configuration.md#agent_skip_update_check-auto-update-opt-out).

- **`--base`** — ignores any `Dockerfile.agent` in the current directory and launches the base `claude-agent` image instead. Project-specific `EXPOSE`, `agent-user`, and `agent-run-args` directives are skipped.

TTY allocation is auto-detected: the container gets a pseudo-TTY (`docker run -it`) only when the wrapper's own stdin is a terminal. When stdin is not a terminal — for example when launched from a script or `subprocess` with `stdin=DEVNULL` or a pipe — it runs non-interactively (`docker run -i`), so `agent run` can be driven headlessly to launch fleets of agents without the `cannot attach stdin to a TTY-enabled container` error.

Because `agent` is a real executable on `PATH` (not a shell function), programmatic callers invoke it directly — no need to source the bashrc into a shell first:

```python
import subprocess

# With <repo>/bin on PATH (or bin/agent symlinked onto PATH):
subprocess.run(["agent", "run", "--base"], stdin=subprocess.DEVNULL, check=True)
```

## `agent rebuild`

```
agent rebuild [--full]
```

Rebuilds the resolved image with `--no-cache`, passing `HOST_UID`/`HOST_GID` build args.

> This command and `agent run` check for wrapper updates on every invocation. See [`AGENT_SKIP_UPDATE_CHECK`](configuration.md#agent_skip_update_check-auto-update-opt-out).

- **`--full`** — rebuilds the base `claude-agent` image first, then the project image. Use this to update the pinned Claude Code CLI version (new releases come out daily), or when the base image is missing.

## `agent create`

```
agent create
```

Scaffolds a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory, pre-populated with a `# agent-name: <sanitized-dirname>` comment line — the same directive [docs/docker-sandboxing.md](docker-sandboxing.md#recognized-directives) documents as required.

## `agent stats`

```
agent stats [--verbose] [--from D] [--until D] [--days N] [--pattern P]
```

Aggregates token usage and estimated USD cost across every project where you've launched `agent run`. Reads the project registry at `<wrap-dir>/.agent-launches/projects.txt` (see [`agent run`](#agent-run) for its on-disk format) and walks each project's `.claude/litellm-logs/` directory (organized by provider and session). Pricing is fetched dynamically per provider as logs are scanned. Both the per-project table and the per-day breakdown cover the same usage window.

Selection range — at most two of the three flags may be combined:

- **`--from D`** — inclusive lower bound; `D` is an absolute date (`YYYY-MM-DD`) or a relative offset (`-Nd`, e.g. `-14d`).
- **`--until D`** — inclusive upper bound; same format as `--from`.
- **`--days N`** — span in days; `N=0` means unlimited (no day bound).

The **`-v`/`--verbose`** flag is independent of the range: it adds a usage-source breakdown table over the same window, splitting the totals by how each request's usage was obtained (read straight from the response, recovered from the request log, or uncountable).

The **`-p`/`--pattern P`** flag filters projects by a regex matched against each project's recorded registry path, independent of the range.

With no flags the window is the last 28 days. `--from` alone runs to now; `--days N` alone is the last N days; `--until` alone spans the 28 days ending at that date; `--days 0` alone shows all time (open lower bound, up to now).

Day buckets default to host-local midnight-to-midnight; see [`AGENT_DAY_START_UTC`](configuration.md#agent_day_start_utc-stats-day-boundary-offset) to change the boundary.

Create an `.agent_stats_leaf` file in a directory to aggregate every registered project at or beneath it into a single **transient project** row, instead of one row per project — handy when a script launches many agents in per-run subdirectories. The group is always named after the marker directory itself; the file's content is irrelevant — only its presence matters. The aggregated row is accented in color (alongside the `<orphaned>` row) to set it apart. The lookup walks the path literally (symlinks are not resolved), so a directory that holds an `.agent_stats_leaf` plus symlinks to several unrelated projects groups them all together.

Logs left behind by a deleted or unregistered project — request logs that survive under `<wrap-dir>/litellm-logs/` after their project is gone from the registry — are gathered into a synthetic `<orphaned>` row so their usage is not silently lost. It appears as its own line (not under the project tree), and its tokens and cost are still included in the per-model and per-day totals. Usage that [`agent cleanup`](#agent-cleanup) archived before deleting such logs folds into the same row, so cleaned-up spend keeps appearing here; because the archive keeps no session identity, it contributes to the token and cost columns but not to `SESSIONS`.

## `agent logs`

```
agent logs [--port N] [--stop]
```

Starts a local, read-only web viewer for the LiteLLM request logs written under each project's `.claude/litellm-logs/` directory. (That path is now a symlink into the shared per-project log store at `<wrap-dir>/litellm-logs/<project_hash>/`, since each provider's sidecar serves every project; the viewer follows it transparently, and a project that has run several providers shows one subtree per provider.) Reads the same project registry as `agent stats` (`<wrap-dir>/.agent-launches/projects.txt`, see [`agent run`](#agent-run) for its on-disk format), then lets you pick a project, pick a session, and read every logged request chat-style: the system prompt, the message thread (including `tool_use`/`tool_result` blocks), the tool definitions, the response, and per-request token usage. Hashed strings (`hash:<sha256>`) are resolved from each session's `strings.jsonl` for display.

Sessions are labelled with their Claude Code alias (the short kebab-case name, e.g. `agent-logs-web-viewer`) when available. The alias is detected from Claude Code's own session-naming call as it passes through the sidecar and persisted to an `alias` file beside the logs; for older logs it is derived on the fly from the same call, falling back to the session UUID when no name exists yet.

The viewer is a host-level singleton that runs **in the background**: `agent logs` prints its connect line (`http://127.0.0.1:<port>`) and returns the shell to you immediately. The server binds to `127.0.0.1` only. Running `agent logs` again while a viewer is already running just reprints the existing connect line — the running port is reused and `--port` is ignored. The background process records its PID and port in `<wrap-dir>/.agent-launches/logs-server.json` (its stdout/stderr go to `logs-server.log` beside it).

The viewer applies the same grouping as `agent stats`: projects under an `.agent_stats_leaf` marker appear as one project whose session list is the union of its members, and an `<orphaned>` project collects sessions from logs with no registered project so you can still read them.

- **`--port N`** — binds the viewer to port N (default `8765`); if that port is busy, it scans up to 50 successive ports for a free one. Ignored when a viewer is already running.
- **`--stop`** — stops the background viewer (no-op with a friendly message if none is running).

## `agent cleanup`

```
agent cleanup [--dry-run]
```

Removes the two kinds of leftover state that accumulate when a registered project is deleted or renamed, both of which [`agent stats`](#agent-stats) already reports but never cleans up:

- **Orphaned log dirs** — `<wrap-dir>/litellm-logs/<hash>/` directories no longer reachable from any registered project's `.claude/litellm-logs` symlink. These hold the raw per-request JSONL and are what actually consumes disk.
- **Stale registry entries** — lines in `<wrap-dir>/.agent-launches/projects.txt` whose project directory no longer has a logs directory (shown as `(missing)` in the stats tree).

Prints how many log dirs it would delete and roughly how much space that frees, then asks for confirmation — it proceeds only on `y`, and cancels on anything else (including a non-interactive stdin). On success it prints a one-line summary with the space actually reclaimed, never the stats table.

Usage is preserved before deletion, so cleaning up does not make historical spend disappear from `agent stats`. Each dir's token counts are merged into an archive at `<wrap-dir>/.agent-launches/orphaned-usage-archive.json`, keyed by UTC date → hour → model → usage source. Deletion is a per-directory two-phase commit: the merged counts are written to a `*.new.json` staging file, the directory is removed, and only then is the staging file promoted over the real archive. A directory that cannot be deleted is therefore never archived — it simply shows up again next run rather than being counted twice. If the final promotion fails, the command stops and tells you the `mv` to run by hand.

The archive deliberately stores raw UTC hours and no cost. Day bucketing (see [`AGENT_DAY_START_UTC`](configuration.md#agent_day_start_utc-stats-day-boundary-offset)) and pricing are re-derived on every `agent stats` run, so changing your day boundary or a provider's prices afterwards still reports archived spend correctly.

- **`--dry-run`** — prints the same counts and size estimate, then exits. Never prompts and never deletes or rewrites anything.

## `agent secrets`

```
agent secrets check|set|clear <sidecar>
agent secrets cleanup
```

Manages secrets in the encrypted store, namespaced per sidecar/provider (e.g. `litellm-bedrock:api_key`, `telegram:TelegramBotToken`).

- **`check <sidecar>`** — reports whether each secret required by `<sidecar>` is present, without revealing values.
- **`set <sidecar>`** — prompts for and persists each secret required by `<sidecar>`.
- **`clear <sidecar>`** — deletes all secrets stored for `<sidecar>`.
- **`cleanup`** — removes any stored keys that don't belong to a known sidecar/provider.

Provider secrets are also resolved interactively on the first `agent run` when stdin is a TTY (they're required, so a missing one triggers a prompt); `agent secrets set <provider>` is the explicit, non-interactive alternative. Telegram secrets are optional and are never prompted for interactively — `agent secrets set telegram` is the only way to set them.

## `agent update`

```
agent update
```

Pulls the latest wrapper source. On `master`, it only updates when a newer tag has been published and fast-forwards to that tag's commit; on any other branch it fast-forwards to the branch tip on any upstream commit. If `default-CLAUDE.md` changed, replaces the user's copy when unmodified; when customized, it leaves the copy untouched and prints instructions for merging or deleting it manually.
