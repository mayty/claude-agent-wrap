<!-- This file has been edited with the assistance of an AI tool. -->
# Shell Commands

`agent` is an executable (`bin/agent`) whose first argument is a verb that selects the operation. All verbs forward to `-m agent_wrap` on the pinned interpreter `bin/agent-bootstrap` provisioned (see [Getting Started](getting-started.md)) and run on the host. The command line is parsed by [click](https://click.palletsprojects.com/), so every verb accepts `-h`/`--help`, and the exit codes follow the POSIX convention: `0` on success, `1` when the operation itself failed, and `2` for a usage error (an unknown flag, a bad value, a missing argument). A bare `agent` prints the verb list as a usage error, so it exits `2`.

Sourcing `agent-wrap.bashrc` adds `bin/` to your `PATH` (so `agent` resolves) and, under bash only, registers tab-completion for the verbs, their flags, and `agent secrets`' sidecar names. Completion is live, not generated: the function in `agent-wrap.bashrc` calls `_AGENT_COMPLETE=bash_complete agent`, which click answers from the same command objects that parse the real command line — there is no generated completion file and no `make` step to keep it in sync. The function itself is click's own, pasted in rather than `eval`'d at shell startup so that opening a shell forks no interpreter. Two consequences worth knowing: flags are offered once you type a `-` (on an empty word there is nothing to complete, since no verb takes a positional), and a flag you have already given is not offered again. Completion is also the one caller that will not provision the interpreter: every other `agent` invocation runs `bin/agent-bootstrap` itself when the venv is missing or was built from different constraints, but a TAB press exits silently instead — and skips the constraints comparison too, so it still costs no fork — because anything on stdout would be read back as a completion candidate.

Programmatic callers that only need to launch `agent` can instead put `<repo>/bin` on `PATH` or symlink `bin/agent` into a directory already on `PATH` — no sourcing required.

| Verb | Purpose |
| --- | --- |
| `run` | Launch Claude Code in a container |
| `rebuild` | Force a rebuild of the project or base image |
| `create` | Scaffold a `.claude-agent-wrap/Dockerfile` |
| `stats` | Aggregate token usage and cost |
| `logs` | Browse LiteLLM request logs in a local web viewer |
| `reindex` | Re-read the LiteLLM log tree into the logs database |
| `inspect` | Report the current state: sidecars, agents, providers, host facts |
| `cleanup` | Delete leftover logs, registry entries and outdated docker images |
| `secrets` | Manage encrypted sidecar/provider secrets |
| `update` | Pull latest wrapper source |

## `agent run`

```
agent run [OPTIONS] [CLAUDE_ARGS]...
```

Launches Claude Code in a Docker container against the resolved image for the current directory. Records the project path in the project registry — the `projects` table of the SQLite database at `<wrap-dir>/.agent-launches/db/projects.db` — for use by `agent stats`. One row per path, with the timestamps of its first and most recent launch. A pre-existing `projects.txt` from before the registry moved to SQLite is imported into the empty table on the next run — `agent run` is one of only two verbs permitted to write the registry, the other being a full `agent cleanup`. The file itself is left untouched and is never read again once the table has rows.

> This command and `agent rebuild` check for wrapper updates on every invocation, except headless `agent run` invocations (`-p`/`--print`/`--bare`/`--safe-mode`). See [`AGENT_SKIP_UPDATE_CHECK`](configuration.md#agent_skip_update_check-auto-update-opt-out).

Before any of that, the launch checks that the current directory is plausibly a project. The whole directory is mounted at `/workspace` and a `.claude/` state tree is written into it, so launching from a home directory or a system root hands the agent a machine to read and drops wrapper state on top of whatever already lives there — in `$HOME`, your own `~/.claude`. Both are done by the time Claude Code starts and neither is undone by quitting it. So `agent run` names the directory in red and asks `Launch here anyway? [y/N]`; **No is the default**, and Enter cancels the launch (exit 0) before an image is resolved or a logs viewer is started. A launch with nobody to answer — headless, or stdin not a terminal — is refused outright with exit 1 rather than prompted. What counts as "not a project" is listed under [`AGENT_SKIP_SAFETY_CHECK`](configuration.md#agent_skip_safety_check-directory-safeguard-opt-out); a directory *inside* one of those, such as `~/projects/thing` or `/usr/local/src/thing`, is never questioned.

Every launch also starts the [`agent logs`](#agent-logs) background viewer, as its very first step, so the viewer's initial walk of the log tree runs while the image is resolved and the sidecar comes up rather than after. It is what keeps the statusline's token and cost segment current, and it is deliberately **not** stopped when the agent exits — the viewer is a host-level singleton shared by every project, so `agent logs --stop` is how you stop it by hand. The one thing that stops it on your behalf is [`agent update`](#agent-update), which does so before it merges; the next launch starts it again. A viewer that is already running or already starting is adopted rather than duplicated, and a failure to start one is a warning rather than an aborted launch. Headless invocations skip it (nothing renders a statusline), as does the `litellm-anthropic-sub` provider (its statusline segment reports subscription rate limits instead). See [`AGENT_AUTOSTART_LOGS`](configuration.md#agent_autostart_logs-logs-viewer-autostart-opt-out).

Images are built on demand, so there is no rebuild to remember. Right after the update check and before any Dockerfile directive is read, `agent run` builds the base `claude-agent` image if it is missing and rebuilds it if the local one carries a different `DOCKER_BUILD_ITERATION` than this checkout does (or carries none at all, which is how an image built before 0.10.0 reads). It then does the same for the project image: every project image records the base image's Docker ID as an `agent-wrap.base-image-id` label at build time, and a mismatch — including the one a base rebuild has just created — means it gets rebuilt on top of the new base. A build the wrapper started on your behalf announces itself with the reason and a note about what it will cost, which differs by image: the base image reuses its scaffolding layers from docker's cache and always reinstalls only the Claude Code CLI, while a project image builds with `--no-cache` and re-runs every `RUN` step. See [Build caching](docker-sandboxing.md#build-caching). This applies to headless launches too: they skip the *update* check because nobody is there to answer it, but a launch with no usable image cannot proceed at all. What is **not** detected is an edit to your own `.claude-agent-wrap/Dockerfile` — nothing hashes that file, so applying such an edit is still an explicit [`agent rebuild`](#agent-rebuild).

Concurrent launches serialize behind one host-global build lock, and each one re-checks staleness after acquiring it: the second launcher finds the first one's image current and builds nothing. A launcher that has to wait says so before it blocks.

- **`-b`/`--base`** — ignores any `.claude-agent-wrap/Dockerfile` in the current directory and launches the base `claude-agent` image instead. Project-specific `EXPOSE`, `agent-user`, `agent-run-args` and `agent-enable-startup` directives are skipped, so the [startup script](docker-sandboxing.md#startup-script) does not run either. Only the base image is built or rebuilt; a project image in the same directory is left exactly as it is.

TTY allocation is auto-detected: the container gets a pseudo-TTY (`docker run -it`) only when the wrapper's own stdin is a terminal. When stdin is not a terminal — for example when launched from a script or `subprocess` with `stdin=DEVNULL` or a pipe — it runs non-interactively (`docker run -i`), so `agent run` can be driven headlessly to launch fleets of agents without the `cannot attach stdin to a TTY-enabled container` error. Such a launch is also the one that the directory safeguard above refuses instead of prompting, so a fleet that genuinely belongs in a home or system directory needs [`AGENT_SKIP_SAFETY_CHECK`](configuration.md#agent_skip_safety_check-directory-safeguard-opt-out) set.

Because `agent` is a real executable on `PATH` (not a shell function), programmatic callers invoke it directly — no need to source the bashrc into a shell first:

```python
import subprocess

# With <repo>/bin on PATH (or bin/agent symlinked onto PATH):
subprocess.run(["agent", "run", "--base"], stdin=subprocess.DEVNULL, check=True)
```

## `agent rebuild`

```
agent rebuild [OPTIONS]
```

Rebuilds the resolved image, passing `HOST_UID`/`HOST_GID` build args. A project image is rebuilt with `--no-cache`; the base image uses docker's layer cache below the Claude Code CLI install, which is reinstalled either way — see [Build caching](docker-sandboxing.md#build-caching). This is the *force*: [`agent run`](#agent-run) already builds what is missing or stale by itself, so what is left for this verb is the rebuild the wrapper cannot infer — most often applying an edit you just made to `.claude-agent-wrap/Dockerfile`. The base image is still ensured underneath, so a project build never runs on an absent or stale base.

> This command and `agent run` check for wrapper updates on every invocation. See [`AGENT_SKIP_UPDATE_CHECK`](configuration.md#agent_skip_update_check-auto-update-opt-out).

- **`-f`/`--full`** — rebuilds the base `claude-agent` image first, then the project image, both unconditionally. Use this to pick up a new Claude Code CLI release (they come out daily) or a changed [`AGENT_SPELLCHECK_LANG`](configuration.md#agent_spellcheck_lang-dictionaries) — neither of which the wrapper can see from outside the image. Picking up a CLI release is cheap: with the scaffold cached, the base rebuild is roughly one `npm install -g`. A changed dictionary list costs more, because `SPELLCHECK_LANG` invalidates the dictionary layer and everything after it. It is not needed to bootstrap a host or to recover a missing base image; `agent run` does both.

## `agent create`

```
agent create [OPTIONS]
```

Scaffolds a minimal `.claude-agent-wrap/Dockerfile` (`FROM claude-agent`) in the current directory, pre-populated with a `# agent-name: <sanitized-dirname>` comment line — the same directive [docs/docker-sandboxing.md](docker-sandboxing.md#recognized-directives) documents as required. It never overwrites: an existing `.claude-agent-wrap/Dockerfile` is an error, and so is a leftover deprecated `Dockerfile.agent`, which it tells you to move rather than scaffolding a second file beside it.

## `agent stats`

```
agent stats [OPTIONS]
```

Aggregates token usage and estimated USD cost across every project where you've launched `agent run`. Reads the project registry at `<wrap-dir>/.agent-launches/db/projects.db` (see [`agent run`](#agent-run)) and the request index at `<wrap-dir>/.agent-launches/db/logs.db` (see [`agent reindex`](#agent-reindex)). Pricing is fetched dynamically per provider as the totals are computed. Both the per-project table and the per-day breakdown cover the same usage window, and are aggregated from one read of the index so they cannot disagree.

It does not open a log file, and it writes nothing — neither database, nor the log tree. So its totals cover exactly what has been indexed. Normally that is everything: the [`agent logs`](#agent-logs) viewer indexes each request as it arrives and [`agent run`](#agent-run) starts the viewer. When it is not, the command says so rather than quietly understating your spend:

```
! 12 of 613 session(s) are behind the log files; totals may understate spend. Run: agent reindex
```

`PROJECT` is drawn as a path tree, so a prefix every project shares is stated once as a dim directory row carrying that subtree's totals, instead of being repeated down the column. When the table does not fit the console, the tree is chopped down to make it fit: that same fold is what turns a fleet living under one parent into a single very wide row, so it is given back one segment at a time — `home/me/work/` becoming `home/` over ` └me/work/` — trading a line of height for a column of width. A whole group of sibling directories is split at once, so a level never renders half folded and the order never shifts under you, and a split that would only push the rows beneath it further right is not taken. Totals are unaffected by how far the tree was chopped. Nothing is ever truncated here: below the width the eight numeric columns need — roughly 95 — the table simply runs past the edge, because every column left holds a date or a figure and half of one of those reads as a wrong number. A report that is piped or redirected is never chopped at all; `COLUMNS` states a width when there is no terminal to ask.

Selection range — at most two of the three flags may be combined:

- **`-f`/`--from D`** — inclusive lower bound; `D` is an absolute date (`YYYY-MM-DD`) or a relative offset (`-Nd`, e.g. `-14d`).
- **`-u`/`--until D`** — inclusive upper bound; same format as `--from`.
- **`-d`/`--days N`** — span in days; `N=0` means unlimited (no day bound).

The **`-r`/`--refresh`** flag re-fetches pricing from the providers' pricing pages instead of using the cached tables, which are otherwise reused for up to 7 days (Bedrock and DeepSeek scrape their pricing pages; DashScope's table is hardcoded and unaffected). Refreshing happens once per provider per run — the re-fetched prices are cached in memory for the rest of the scan.

The **`-p`/`--pattern P`** flag filters projects by a regex matched against each project's recorded registry path, independent of the range.

With no flags the window is the last 28 days. `--from` alone runs to now; `--days N` alone is the last N days; `--until` alone spans the 28 days ending at that date; `--days 0` alone shows all time (open lower bound, up to now).

Day buckets default to host-local midnight-to-midnight (or [`AGENT_TIMEZONE`](configuration.md#agent_timezone-display-timezone)'s midnight, if set); see [`AGENT_DAY_START_UTC`](configuration.md#agent_day_start_utc-stats-day-boundary-offset) to override the boundary directly.

Create an `.agent_stats_leaf` file in a directory to aggregate every registered project at or beneath it into a single **transient project** row, instead of one row per project — handy when a script launches many agents in per-run subdirectories. The group is always named after the marker directory itself; the file's content is irrelevant — only its presence matters. The aggregated row is accented in color (alongside the `<orphaned>` row) to set it apart. The lookup walks the path literally (symlinks are not resolved), so a directory that holds an `.agent_stats_leaf` plus symlinks to several unrelated projects groups them all together.

Logs left behind by a deleted or unregistered project — request logs that survive under `<wrap-dir>/litellm-logs/` after their project is gone from the registry — are gathered into a synthetic `<orphaned>` row so their usage is not silently lost. It appears as its own line (not under the project tree), and its tokens and cost are still included in the per-model and per-day totals. A project's ownership of its logs is resolved through its own `.claude/litellm-logs` symlink, so a project whose directory has been deleted owns nothing and its spend moves to this row. Once [`agent cleanup`](#agent-cleanup) deletes those logs the spend stops appearing at all — the index forgets a project along with its directory.

## `agent logs`

```
agent logs [OPTIONS]
```

Starts a local, read-only web viewer for the LiteLLM request logs written under each project's `.claude/litellm-logs/` directory. (That path is now a symlink into the shared per-project log store at `<wrap-dir>/litellm-logs/<project_hash>/`, since each provider's sidecar serves every project; the viewer follows it transparently, and a project that has run several providers shows one subtree per provider.) Reads the same project registry as `agent stats` (`<wrap-dir>/.agent-launches/db/projects.db`, see [`agent run`](#agent-run)), then lets you pick a project, pick a session, and read every logged request chat-style: the system prompt, the message thread (including `tool_use`/`tool_result` blocks), the tool definitions, the response, and per-request token usage. Long strings that the sidecar replaced with a `hash:<sha256>` pointer are shown in full: the original is stored in the index and travels to the browser with the message that quotes it.

The project and session lists come from the request index at `<wrap-dir>/.agent-launches/db/logs.db`, which the viewer keeps current itself — see [`agent reindex`](#agent-reindex). A session it has not indexed is not listed, and the viewer indexes each request as it arrives, so in practice that is the moment the request is logged. A session served by more than one provider, or shared by the members of a grouped project, is listed once with its request counts added together.

A session you open is read out of the same index, in one request. Its content is sent once each and referenced by the requests that use it, which matters because a conversation prefix is re-sent to the model on every turn: the longest session in a 2.2 GB log tree is 910 requests holding 131,000 message references, behind which stand 2,442 distinct messages. Opening it transfers 12 MB rather than 59 MB, and the once-a-second refresh that follows transfers only the new requests and whatever content they introduced — tens of kilobytes, whatever the length of the session behind them.

Sessions are labelled with their Claude Code alias (the short kebab-case name, e.g. `agent-logs-web-viewer`) when available. The alias is read out of Claude Code's own session-naming call as that call passes through the sidecar, and kept in the index; a session that has not been named yet is labelled with its UUID.

The viewer is a host-level singleton that runs **in the background**: `agent logs` prints its connect line (`http://127.0.0.1:<port>`) and returns the shell to you immediately. The server binds to `127.0.0.1` only. Running `agent logs` again while a viewer is already running just reprints the existing connect line — the running port is reused and `-p`/`--port` is ignored. The background process records its PID and port in `<wrap-dir>/.agent-launches/logs-server.json` (its stdout/stderr go to `logs-server.log` beside it).

Most of the time the viewer is already up, because [`agent run`](#agent-run) starts it. A viewer that has been claimed but has not bound its port yet is reported as starting rather than started a second time; its recorded port is provisional at that point, since the bind scans upward when the port is taken. Two launches racing from a stopped state produce one viewer, not two: the decision to spawn is taken under a lock (`logs-server.lock`, beside the state file), and the loser adopts the winner's viewer. The viewer is handed off to `init` rather than parented to whichever process started it, so an `agent run` that outlives it by hours leaves no zombie behind.

The viewer applies the same grouping as `agent stats`: projects under an `.agent_stats_leaf` marker appear as one project whose session list is the union of its members, and an `<orphaned>` project collects sessions from logs with no registered project so you can still read them.

Updates are driven by filesystem events rather than by a timer, so a request shows up in the browser within a fraction of a second of being logged, and an idle viewer costs nothing — it stops re-walking the log tree entirely instead of doing so every two seconds. One watch covers the shared log store at `<wrap-dir>/litellm-logs/`, which is where every sidecar writes, and one covers the project registry. A full reconciliation still runs once a minute, which is what keeps `usage.json` fresh for the statusline, rolls the daily totals over at the day boundary, and picks up a project registered since the last pass. A project [`agent cleanup`](#agent-cleanup) removed leaves the lists as soon as its index rows go, which happens with the directory. This is why the wrapper has to be installed on a local filesystem — see [Setup](getting-started.md#setup); `agent logs` warns if it is not.

- **`-p`/`--port N`** — binds the viewer to port N (default `8765`); if that port is busy, it scans up to 50 successive ports for a free one. Ignored when a viewer is already running.
- **`-s`/`--stop`** — stops the background viewer (no-op with a friendly message if none is running). It takes no other argument, and `--port` alongside it is refused even when the port given is the default one.

## `agent reindex`

```
agent reindex [OPTIONS]
```

Reads the LiteLLM log tree at `<wrap-dir>/litellm-logs/` into the logs database at `<wrap-dir>/.agent-launches/db/logs.db`, and reports how much of it was new. The database is what [`agent stats`](#agent-stats) and the [`agent logs`](#agent-logs) viewer read — neither of them opens a log file — so a request that has not been indexed does not appear in either. Normally nothing needs indexing by hand: the background viewer ingests each request as it is logged, and [`agent run`](#agent-run) starts the viewer. This verb is for the cases where that did not happen — logs that predate the database, a host where the viewer has never run, or a stretch of launches made with [`AGENT_AUTOSTART_LOGS`](configuration.md#agent_autostart_logs-logs-viewer-autostart-opt-out) turned off.

Indexing is incremental and safe to repeat. Each session records how many bytes of its own `messages.jsonl` and `strings.jsonl` have been read, so a second run reads nothing past those marks and reports zero sessions changed rather than double-counting anything; the summary line names both figures (`indexed 0 request(s) from 0 of 613 session(s)`) precisely so that "already up to date" is distinguishable from "found nothing". Both files are append-only, so a file that is *shorter* than its recorded mark was replaced rather than appended to; such a session is dropped and read again from the start, and the count of those is reported separately.

A session that cannot be read is named and the walk continues — one unreadable directory does not cost the other several hundred — but the command still exits `1` so a script notices. Ingest is serialized host-wide by a `logs-ingest.lock` in `<wrap-dir>/.agent-launches/`, and the lock is taken without waiting: if the viewer or another `agent reindex` is already reading the tree, this one prints a warning, does nothing, and exits `1`, because whoever holds the lock is already doing the same work.

This is one of the two verbs permitted to write the logs database, the other being the [`agent logs`](#agent-logs) viewer itself. It never writes the project registry, and — unless you pass `--prune` — it never writes into the log tree it reads.

- **`--prune`** — after indexing, apply the retention set by [`AGENT_LOGS_RETENTION_DAYS`](configuration.md#agent_logs_retention_days-request-log-retention): every session with no activity for that many days has its log files and its index rows deleted, and the content behind those rows is reclaimed by the same [index sweep](#retention-and-the-index-sweep) `agent cleanup` runs. This is permanent, and there is no prompt — the flag and the variable are both things you had to say. Without the variable set the flag is refused as a usage error rather than treated as a no-op, since with no age to compare against there is nothing it could prune. Indexing happens first on purpose: a session's age is the age of the newest request the *index* holds, so pruning before the pass that updates it would judge a session by a watermark that was about to move.

## `agent inspect`

```
agent inspect [OPTIONS]
```

Reports what agent-wrap is doing on this host right now. Answers the questions that otherwise need `docker ps`, `docker inspect`, and a look inside `<wrap-dir>/.agent-launches/`: which sidecars are up, which agents are attached to which sidecar, whether the logs viewer is alive, and whether each provider's secrets are in place.

The output is four tables:

- **Sidecars** — one row per `agent-wrap-*` container: its role (`litellm` or `telegram`), the image it runs, Docker state, health-check verdict, uptime, listening port, and the number of live agents attached. The image is shortened to its name and tag, because sidecars are pinned by digest and the full reference is too long for the row; `-j`/`--json` carries the reference in full. Stopped containers are listed too, with their exit code — the Telegram sidecar deliberately runs without `--rm` so a crash during startup leaves its logs inspectable, and that corpse is worth seeing. A container running an image other than the current pin is marked `(stale image)`; restart it to adopt the pin. Two footnotes can follow the table, both facts about the shared sidecar lock: how many running sidecars have no agents attached, and how many launches are queued waiting for the lock.
- **Agents** — one row per `claude-agent-*` container: its image, the host directory mounted at `/workspace`, the provider its model traffic goes through, its Docker state, and its uptime. Rows are ordered by image then directory, which groups a fleet the way its owner thinks about it — the container name is an instance id, so ordering by it would be random. The provider comes from the flock registry under `.agent-launches/running/`, not from Docker: an agent's environment holds its provider's credentials but never the provider's name. An agent with no live registration (a headless run, or one already tearing down) shows `—`.
- **Details** — everything that is not a container, in three groups separated by dividers: the logs viewer, whether the next [`agent run`](#agent-run) would start it, the on-disk log footprint and the request index's; per-provider secret readiness with the default provider marked; and the installed wrapper revision plus the host facts behind most launch surprises (base-image and network presence, whether `AGENT_USE_HOST_NETWORK` is actually in effect, whether the launch-directory guard is still on, and the resolved day boundary). Each image row also carries the Claude Code version installed inside it, and the base-image row is flagged when the npm registry has a newer one. Directly under it, a `project image` row appears whenever the current directory declares a [`.claude-agent-wrap/Dockerfile`](docker-sandboxing.md) — the `claude-agent-<agent-name>` image `agent run` would actually launch here, so a project image left behind by an `agent rebuild --full` that skipped the project build is visible rather than merely surprising later. Either image row is annotated **STALE** with the reason the next `agent run` would rebuild it, so the rebuild that is coming is stated before you pay for it. A project that customizes nothing gets no such row. Secret readiness is one of two states, `Secrets OK` or `Secrets NOT SET` — [`agent secrets check`](#agent-secrets) names the individual keys, and `-j`/`--json` still lists them. The day boundary is stated as an offset from UTC midnight, e.g. `-3h UTC`, with the source noted when it comes from [`AGENT_DAY_START_UTC`](configuration.md#agent_day_start_utc-stats-day-boundary-offset) or [`AGENT_TIMEZONE`](configuration.md#agent_timezone-display-timezone) rather than the host's local offset. The `directory guard` row is `on`, or `OFF (AGENT_SKIP_SAFETY_CHECK)` when the [safeguard](configuration.md#agent_skip_safety_check-directory-safeguard-opt-out) has been turned off; it reports the setting only, and says nothing about the directory `agent inspect` itself was run from. The `request index` row sits directly under `logs storage`, and the pair is the point of it: the log tree is the append-only source of truth, and the index is what every consumer reads. It reports the index's own size, how many sessions and requests it holds, and how long ago it was last written to — an age rather than a date, since the question is whether ingest is keeping up. A host that has never ingested reads `never ingested` rather than an age of zero. When indexed sessions have fallen behind their log files, the row turns yellow, names the count and points at [`agent reindex`](#agent-reindex); `-l`/`--lite` does not measure that at all and then says nothing about it, which is not the same as saying it is current.
- **Stale images** — one row per registered project (`<wrap-dir>/.agent-launches/db/projects.db`) whose *own* `claude-agent-<name>` image would be rebuilt on its next [`agent run`](#agent-run), with the reason. This is the fleet-wide version of the `project image` row above: it answers "which of my projects will pay for a rebuild?" without visiting each one. Two projects that declare the same `# agent-name:` are two rows sharing one image, because the project is what you act on. `PROJECT` is drawn as a path tree with shared prefixes collapsed, the same rendering the [`agent stats`](#agent-stats) projects table uses: registered projects cluster under a few parents, and this column is measured rather than capped, so the deepest path would otherwise set the width of the whole table. When the console cannot take the whole table, `IMAGE` and `REASON` give up characters and end in `…`, longest first, down to their own headings — and only they do. `PROJECT` is never cut: a path is what you act on and half of one identifies nothing, so when the tree itself is what will not fit, the tree is chopped down instead, one segment at a time and a sibling group at a time, exactly as the [`agent stats`](#agent-stats) projects table does. Chopping is reserved for that case rather than run whenever the table is wide, because spending a row per path segment to win a sentence a few more characters is a poor trade. `-j`/`--json` carries every reason in full however narrow the console is. None of this applies when the output is not a terminal, so a piped or redirected report is unchanged; `COLUMNS` states a width explicitly when there is no terminal to ask. The dim rows the tree adds are directories, not projects -- they carry no image and no reason, and the row count in the title stays the number of stale projects. Three kinds of project are deliberately absent rather than listed as current: one that declares no [`.claude-agent-wrap/Dockerfile`](docker-sandboxing.md) (its target is the base image, already reported once on the `base image` row — a row per plain project would only restate it), one whose image is not built on this host (nothing is stale about an image that does not exist, and the launch that creates it is not a rebuild), and one whose directory or Dockerfile cannot be read from here — which, run from inside an agent container, is every project but the mounted one. When nothing is stale the table is replaced by one green line saying so, rather than drawn empty. A stale base image condemns every project image built on it, so that case is reported without inspecting them individually and the table lists them all at once.

> A sidecar showing `0` attached agents is normal, not a leak. Teardown drops every registration before it takes the lock to stop the container, so there is a legitimate window in which a running sidecar has no registered agent.

The report says nothing about token usage or cost — that is [`agent stats`](#agent-stats)' job, and it answers the question properly rather than from whatever a running viewer last happened to record.

The command is read-only: it starts no agent, stops nothing, and writes no data. It takes no write permission on either database, so no read it performs can migrate one's *contents*; opening the request index does converge its schema, which is how any consumer of a database that does not exist yet gets one. In particular it does not reap stale lock files and does not repair a stale `logs-server.json`, both of which the equivalent launch-path code paths do. Read-only is not the same as free, though: reading the Claude Code version inside an image means starting a throwaway container from it, and the "is there a newer one" check runs `npm view` in that container, which reaches the npm registry — `-l`/`--lite` is what drops it. The stale-image sweep is cheap per project but unbounded in the registry's size: one `docker image inspect` per *distinct* project image, and none at all when the base image is already stale. The wrapper revision is always read locally, unlike [`agent update`](#agent-update), so it is the installed one.

- **`-j`/`--json`** — emits the whole report as one JSON document instead of tables, for scripting and fleet monitoring. Only secret *names* ever appear (the same ones `agent secrets check` prints); no secret value, master key, or upstream token is ever read into the report.
- **`-l`/`--lite`** — skips the four slowest steps and reports everything else: the npm-registry check (so no `→ v… available` flag, and `latest_claude_version` is `null`), the recursive walk over the shared logs tree (so `logs storage` reads `not measured (--lite)` and `logs_bytes` is `null` — which is not the same as zero), the ingest-lag walk that stats every indexed session's log file (so the `request index` row states its size and counts but says nothing about being behind, and `index_behind` is `null` — again not the same as zero), and the stale-image sweep over the project registry (so the table and its green replacement line are both absent, and `stale_images` is `null` — which is not the same as `[]`, the measured verdict that every project image is current). Both Claude Code versions, both container tables, and every filesystem section are still reported, and the report closes with a line naming what was skipped. Written for a project [`startup.sh`](docker-sandboxing.md), which runs while holding the host-global startup lock. Combines with `-j`/`--json`.

Exits `1` when the Docker daemon cannot be reached, after printing every section that does not depend on Docker (`-j`/`--json` still emits a parseable document with `"docker": {"available": false}`). Otherwise exits `0`.

## `agent cleanup`

```
agent cleanup [OPTIONS]
```

Removes the kinds of leftover state that accumulate as projects are deleted or renamed and images are rebuilt. The first two are things [`agent stats`](#agent-stats) already reports but never cleans up; the third overlaps what [`agent inspect`](#agent-inspect) reports as stale; the fourth appears only if you have asked for it:

- **Orphaned log dirs** — `<wrap-dir>/litellm-logs/<hash>/` directories no longer reachable from any registered project's `.claude/litellm-logs` symlink. These hold the raw per-request JSONL and are what actually consumes disk. Their rows in the request index go with them, so their spend stops appearing under `<orphaned>` in [`agent stats`](#agent-stats) — the preview says so before asking. The directory is removed first and its rows second, and only for directories actually removed: a delete that fails leaves that directory live, still orphaned, and still counted next time.
- **Stale registry entries** — rows in `<wrap-dir>/.agent-launches/db/projects.db` whose project directory no longer has a logs directory (shown as `(missing)` in the stats tree).
- **Outdated docker images** — see [Outdated images](#outdated-images) below.
- **Expired sessions**, when [`AGENT_LOGS_RETENTION_DAYS`](configuration.md#agent_logs_retention_days-request-log-retention) is set — sessions with no activity for that many days, whichever project they belong to. Retention is off unless you set that variable, and it is surveyed like the three above rather than merely announced: the preview names the count, the age and the size before the prompt. See [Retention and the index sweep](#retention-and-the-index-sweep).

A fifth thing is reclaimed on every run rather than surveyed first — content in the request index that no request reaches any more. See [Retention and the index sweep](#retention-and-the-index-sweep).

Prints how many log dirs it would delete, roughly how much space that frees and that their spend will stop being reported, tables every image it would remove, names any sessions retention would expire, says that unreachable index content will be reclaimed (see [Retention and the index sweep](#retention-and-the-index-sweep)), then asks for confirmation **once** covering all of it — it proceeds only on `y`, and cancels on anything else (including a non-interactive stdin). On success it prints a one-line summary with the space actually reclaimed and the number of images removed, then a line for retention when it deleted anything and one for the sweep when it reclaimed anything, and never the stats table.

### Outdated images

Four kinds of image qualify. Ownership is decided by **name**, never by a label being present: docker merges `Config.Labels` through `FROM`, so an image you build on `claude-agent` carries the wrapper's labels too, and everything built before 0.10.0 carries none at all.

- **Superseded builds** — untagged (`<none>`) images left behind whenever a tag is rebuilt, including by a manual [`agent rebuild`](#agent-rebuild). Docker takes the repository away along with the tag, so there would be no name left to match on; each wrapper build therefore records the tag it was built as in an `agent-wrap.image` label, and an untagged image carrying one is by definition not the live image for that name. This is usually the largest reclaim.
- **Orphaned project images** — `claude-agent-<name>` tags that no registered project resolves to any more, because the project was deleted or changed its `# agent-name:`.
- **Stale project images** — images a launch would rebuild anyway, exactly as `agent inspect` reports them. Removing one defers no work that was not already owed, and the preview says so in a note under the table whenever it lists one. Note that right after a wrapper release bumps `DOCKER_BUILD_ITERATION`, *every* project image reads as stale, so a cleanup then removes all of them.
- **Superseded sidecar images** — pulled LiteLLM/Telegram images left resident after the wrapper's pinned digest moved. A row whose digest docker does not know is left alone rather than guessed at.

Three things are never removed:

- **The base `claude-agent` image**, even when it is stale. Every project image descends from it, so `docker rmi` would merely untag it — reclaiming nothing, creating a fresh untagged image, and leaving the next launch a cold-scaffold rebuild.
- **Images a container still references.** Removal never passes `--force`, so docker's refusal stands; each one is reported as a warning and the rest of the run continues.
- **Untagged images carrying no `agent-wrap.image` label.** A wrapper build from before that label existed (pre-0.10.0) and a leftover from your own unrelated `docker build` are indistinguishable, so they are counted and pointed at `docker image prune` instead. The one-time rebuild every image gets on the first 0.10.0 launch is what stamps the label, so this only ever describes leftovers already on disk before that.

Sizes are shown per image as docker reports them and are deliberately never totalled: images share layers, so a sum would overstate the reclaim badly. Two caveats worth knowing: a project directory that cannot be read (an unmounted drive, say) contributes no claimed name, so its image reads as orphaned — the cost is one rebuild, and every row is listed before you confirm. And an untagged image *you* built `FROM claude-agent-<name>` inherits the label and is swept with the rest. Nothing happens at all when the Docker daemon is unreachable.

### Retention and the index sweep

Every real run finishes with one reclaim pass over the request index: retention first, then the sweep. Both are skipped together when another process is ingesting, because both are done under the ingest lock — the run says so, and nothing is lost.

Retention does nothing at all unless [`AGENT_LOGS_RETENTION_DAYS`](configuration.md#agent_logs_retention_days-request-log-retention) is set. When it is, a session is expired once its **newest** request is older than that many days, so a long-running session is as new as its last call however old its first was. An expired session's log files are deleted along with its index rows, which is what makes retention hold: the index re-reads any directory it has forgotten from byte zero, so dropping the rows alone would undo the prune on the very next pass and pay for it twice. Two sessions are never touched — one whose records carry no timestamp at all, since there is no age to compare, and one whose `messages.jsonl` has grown past what the index has read, since deleting that would destroy requests no consumer has ever seen. The second check is made again at the moment of deletion rather than trusted from the survey.

The sweep then reclaims content in the request index that nothing reaches any more, and returns the pages to the database file. It is the only thing that ever does: a session whose log file was replaced rather than appended to is re-indexed from zero and leaves its old content behind, and deleting a project's rows leaves everything only that project's requests pointed at.

Reachability is asked from scratch rather than counted as rows come and go, and it is asked three ways. A request names content by id — its message vector and its system, tools and response columns. It also names it by *text*: the sidecar interns every string of 70 characters or more, replacing it with a `hash:<sha256>` pointer, so most of a conversation's actual words are reached only by a pointer inside another piece of content — or, for a failure's message, by one inside the request row's own `error` column. Those pointers are inside compressed payloads that SQL cannot look into, so deciding what is unreachable costs a decompression pass over everything a request still points at. That is why it runs here, on a verb you invoked to reclaim space, and never on a timer.

The counts are not part of the preview, because finding out how much it would reclaim costs exactly the same pass as reclaiming it. The preview says that it happens; the summary says what it found. A run that finds another process ingesting skips both halves of the pass and says so — nothing is lost, and the next `agent cleanup` reclaims it.

Spend from a deleted project is **not** preserved. Before 0.11.0 each dir's token counts were copied into `<wrap-dir>/.agent-launches/orphaned-usage-archive.json` and kept appearing under `<orphaned>` forever; that archive is gone, and an existing file is left where it is and never read or removed. The preview names this consequence before it asks.

- **`-n`/`--dry-run`** — prints the same counts, size estimate, image table and retention line, then exits. Never prompts and never deletes, removes or rewrites anything. That includes the project registry, so on a fresh upgrade whose `projects.txt` has not been imported yet the preview reads *every* log directory as orphaned: with no registered projects, none of them is reachable from one. The command says so on stderr, and a full `agent cleanup` imports the registry before it surveys, so the run you actually confirm describes the right set.

## `agent secrets`

```
agent secrets check|set|clear [OPTIONS] SIDECAR
agent secrets cleanup [OPTIONS]
```

Manages secrets in the encrypted store, namespaced per sidecar/provider (e.g. `litellm-bedrock:api_key`, `telegram:TelegramBotToken`).

This verb is a command group of its own, so each action below has its own `--help` (`agent secrets set --help`) and its own argument list. The three that operate on one sidecar require the name; `cleanup` takes none. Getting that wrong is a usage error (exit `2`), and `SIDECAR` tab-completes from the sidecars the wrapper knows about.

- **`check SIDECAR`** — reports whether each secret required by `SIDECAR` is present, without revealing values.
- **`set SIDECAR`** — prompts for and persists each secret required by `SIDECAR`.
- **`clear SIDECAR`** — deletes all secrets stored for `SIDECAR`.
- **`cleanup`** — removes any stored keys that don't belong to a known sidecar/provider.

Provider secrets are also resolved interactively on the first `agent run` when stdin is a TTY (they're required, so a missing one triggers a prompt); `agent secrets set <provider>` is the explicit, non-interactive alternative. Telegram secrets are optional and are never prompted for interactively — `agent secrets set telegram` is the only way to set them.

## `agent update`

```
agent update [OPTIONS]
```

Pulls the latest wrapper source. On `master`, it only updates when a newer tag has been published and fast-forwards to that tag's commit; on any other branch it fast-forwards to the branch tip on any upstream commit. If `default-CLAUDE.md` changed, replaces the user's copy when unmodified; when customized, it leaves the copy untouched and prints instructions for merging or deleting it manually.

The update is **refused outright while anything is still running** — any agent container (`claude-agent-<id>`) or sidecar (`agent-wrap-<provider>`, `agent-wrap-telegram`) that Docker reports as `running`. It exits `1` after listing what it found, and nothing is merged or re-provisioned — the `git fetch` that discovered the update has already run by then, on this path as much as on the automatic one. Stopping those containers is the only way past it: there is no override flag, because an update rewrites the checkout every live agent's host-side code runs from and re-provisions the interpreter underneath it. Stopped containers are ignored, including the corpse the Telegram sidecar leaves on purpose, and a host whose Docker daemon is unreachable is treated as having nothing running. The same refusal applies to the automatic check inside [`agent run`](#agent-run) and [`agent rebuild`](#agent-rebuild), which exit `1` rather than launching or building.

Once an update is going ahead it stops the [`agent logs`](#agent-logs) background viewer *before* the fast-forward, so that long-lived process never spends the merge window executing newly merged code on an interpreter that has not been re-provisioned yet. It is not restarted here — the next `agent run` starts it again. An update with nothing to pull touches neither Docker nor the viewer.
