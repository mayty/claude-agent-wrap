<!-- This file has been edited with the assistance of an AI tool. -->
# Shell Commands

`agent` is an executable (`bin/agent`) whose first argument is a verb that selects the operation. All verbs forward to `python3 -m agent_wrap` and run on the host. Sourcing `agent-wrap.bashrc` adds `bin/` to your `PATH` (so `agent` resolves) and, under bash only, registers tab-completion for the verbs and their flags. Completion reads its data from the git-tracked `agent-wrap-completion.bash`, which is compiled from each command module's `USAGE` by `scripts/gen-bash-completion.py` (run `make gen-completion` after changing a command's flags). Programmatic callers that only need to launch `agent` can instead put `<repo>/bin` on `PATH` or symlink `bin/agent` into a directory already on `PATH` — no sourcing required.

| Verb | Purpose |
| --- | --- |
| `run` | Launch Claude Code in a container |
| `rebuild` | Rebuild the project or base image |
| `create` | Scaffold a `Dockerfile.agent` |
| `stats` | Aggregate token usage and cost |
| `logs` | Browse LiteLLM request logs in a local web viewer |
| `update` | Pull latest wrapper source |

## `agent run`

```
agent run [--base] [claude-code-args...]
```

Launches Claude Code in a Docker container against the resolved image for the current directory. Records the project path in `<wrap-dir>/.agent-launches/projects.txt` for use by `agent stats`.

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

Scaffolds a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory.

## `agent stats`

```
agent stats [--verbose] [--from D] [--until D] [--days N]
```

Aggregates token usage and estimated USD cost across every project where you've launched `agent run`. Reads the project registry at `<wrap-dir>/.agent-launches/projects.txt` and walks each project's `.claude/litellm-logs/` directory (organized by provider and session). Pricing is fetched dynamically per provider as logs are scanned. Both the per-project table and the per-day breakdown cover the same usage window.

Selection range — at most two of the three flags may be combined:

- **`--from D`** — inclusive lower bound; `D` is an absolute date (`YYYY-MM-DD`) or a relative offset (`-Nd`, e.g. `-14d`).
- **`--until D`** — inclusive upper bound; same format as `--from`.
- **`--days N`** — span in days; `N=0` means unlimited (no day bound).

The **`-v`/`--verbose`** flag is independent of the range: it adds a usage-source breakdown table over the same window, splitting the totals by how each request's usage was obtained (read straight from the response, recovered from the request log, or uncountable).

With no flags the window is the last 28 days. `--from` alone runs to now; `--days N` alone is the last N days; `--until` alone spans the 28 days ending at that date; `--days 0` alone shows all time (open lower bound, up to now).

Create an `.agent_stats_leaf` file in a directory to aggregate every registered project at or beneath it into a single **transient project** row, instead of one row per project — handy when a script launches many agents in per-run subdirectories. The first non-empty line of the file is the project's display name (falling back to the marker directory's name when the file is empty), and the aggregated row is accented in color (alongside the `<orphaned>` row) to set it apart. The lookup walks the path literally (symlinks are not resolved), so a directory that holds an `.agent_stats_leaf` plus symlinks to several unrelated projects groups them all together.

Logs left behind by a deleted or unregistered project — request logs that survive under `<wrap-dir>/litellm-logs/` after their project is gone from the registry — are gathered into a synthetic `<orphaned>` row so their usage is not silently lost. It appears as its own line (not under the project tree), and its tokens and cost are still included in the per-model and per-day totals.

## `agent logs`

```
agent logs [--port N] [--stop]
```

Starts a local, read-only web viewer for the LiteLLM request logs written under each project's `.claude/litellm-logs/` directory. (That path is now a symlink into the shared per-project log store at `<wrap-dir>/litellm-logs/<project_hash>/`, since a single sidecar serves every project; the viewer follows it transparently.) Reads the same project registry as `agent stats` (`<wrap-dir>/.agent-launches/projects.txt`), then lets you pick a project, pick a session, and read every logged request chat-style: the system prompt, the message thread (including `tool_use`/`tool_result` blocks), the tool definitions, the response, and per-request token usage. Hashed strings (`hash:<sha256>`) are resolved from each session's `strings.jsonl` for display.

Sessions are labelled with their Claude Code alias (the short kebab-case name, e.g. `agent-logs-web-viewer`) when available. The alias is detected from Claude Code's own session-naming call as it passes through the sidecar and persisted to an `alias` file beside the logs; for older logs it is derived on the fly from the same call, falling back to the session UUID when no name exists yet.

The viewer is a host-level singleton that runs **in the background**: `agent logs` prints its connect line (`http://127.0.0.1:<port>`) and returns the shell to you immediately. The server binds to `127.0.0.1` only. Running `agent logs` again while a viewer is already running just reprints the existing connect line — the running port is reused and `--port` is ignored. The background process records its PID and port in `<wrap-dir>/.agent-launches/logs-server.json` (its stdout/stderr go to `logs-server.log` beside it).

The viewer applies the same grouping as `agent stats`: projects under an `.agent_stats_leaf` marker appear as one project whose session list is the union of its members, and an `<orphaned>` project collects sessions from logs with no registered project so you can still read them.

- **`--port N`** — binds the viewer to port N (default `8765`); if that port is busy, it scans up to 50 successive ports for a free one. Ignored when a viewer is already running.
- **`--stop`** — stops the background viewer (no-op with a friendly message if none is running).

## `agent update`

```
agent update
```

Pulls the latest wrapper source. On `master`, it only updates when a newer tag has been published and fast-forwards to that tag's commit; on any other branch it fast-forwards to the branch tip on any upstream commit. If `default-CLAUDE.md` changed, replaces the user's copy when unmodified or prompts when customized.
