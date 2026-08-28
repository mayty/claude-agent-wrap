<!-- This file has been edited with the assistance of an AI tool. -->
# Volume Mounts

The `agent run` command bind-mounts several categories of paths into the container:

## Global Claude config

| Host | Container |
| --- | --- |
| `<wrap-dir>/.claude_config/.claude.json` | `/home/<user>/.claude.json` |
| `<wrap-dir>/.claude_config/.claude` | `/home/<user>/.claude` |

The global config directory holds `settings.json`, `CLAUDE.md`, themes, and caches. It is shared across all projects.

## Project files

| Host | Container |
| --- | --- |
| `$(pwd)` | `/workspace` |

The project root is mounted read-write. This is where the agent reads and writes code.

## Per-project state

Each of these paths is overlaid on top of the global `.claude` mount so its contents are visible only within the current project:

| Host | Container |
| --- | --- |
| `$(pwd)/.claude/sessions` | `/home/<user>/.claude/projects/-workspace` |
| `$(pwd)/.claude/memory` | `/home/<user>/.claude/projects/-workspace/memory` |

The following subdirectories and files are mapped to the same path on both sides (e.g., `$(pwd)/.claude/jobs` → `/home/<user>/.claude/jobs`):

- **Directories**: `jobs`, `plans`, `todos`, `tasks`, `shell-snapshots`, `session-env`, `file-history`, `paste-cache`, `image-cache`
- **Files**: `history.jsonl`

`history.jsonl` is the only file mounted this way, and only because it is append-only. A
single-file bind mount pins the inode, so a writer that replaces the file via `rename()` — or
unlinks it — fails with `EBUSY`. Anything written that way needs a directory mount instead.

Two further per-project mounts land **outside** the Claude home directory:

| Host | Container |
| --- | --- |
| `$(pwd)/.claude/claude-tmp` | `/tmp/claude-<uid>` |
| `$(pwd)/.claude/mcp-logs` | `/home/<user>/.cache/claude-cli-nodejs/-workspace` |

`claude-tmp` carries Claude Code's per-session temp tree — `<session-uuid>/scratchpad/` (where the
agent is told to put all temporary files) and `<session-uuid>/tasks/` (background-command output
buffers, plus symlinks to subagent transcripts). Without it, a resumed session's transcript refers
to scratchpad paths that no longer exist. `scratchpad/` and `tasks/` sit under a session UUID minted
at runtime, so they cannot be given separate bind mounts; the tree is mounted as one unit.

`<uid>` is the container's *effective* UID — `0` under rootless Docker, your host UID otherwise —
matching the `--user` flag described in [Notes](#notes).

`mcp-logs` carries each MCP server's stderr log, so a failing MCP server can be diagnosed after the
container exits. The base image pre-creates `~/.cache/claude-cli-nodejs` owned by the agent user:
Docker materializes missing bind-mount *parents* as `root:root`, which would otherwise leave the
agent unable to write anything else under `~/.cache`. A project Dockerfile that sets a custom
`# agent-user:` must pre-create that path itself — see
[Docker Sandboxing](docker-sandboxing.md#recognized-directives).

Neither mount is garbage-collected. Per-session directories accumulate under
`$(pwd)/.claude/claude-tmp/`; prune them yourself if they grow.

On launch the wrapper also creates `$(pwd)/.claude/litellm-logs` as a **symlink** (not a bind mount) pointing at this project's slice of the LiteLLM request logs under `<wrap-dir>/litellm-logs/<project_hash>/`, so the `agent logs` viewer reads them through the project's own `.claude/`. That store is shared across projects *and* providers — every provider's sidecar mounts it, and records land under `<project_hash>/<provider>/`.

## Per-container state

Claude Code's daemon state is **not** shared between agents, not even two agents launched in the
same directory. Each container gets a private subtree keyed by its instance id:

| Host | Container |
| --- | --- |
| `$(pwd)/.claude/instances/<instance-id>/daemon` | `/home/<user>/.claude/daemon` |
| `$(pwd)/.claude/instances/<instance-id>/session-state` | `/home/<user>/.claude/sessions` |
| `$(pwd)/.claude/instances/<instance-id>/daemon.lock` | `/home/<user>/.claude/daemon.lock` |
| `$(pwd)/.claude/instances/<instance-id>/daemon.log` | `/home/<user>/.claude/daemon.log` |
| `$(pwd)/.claude/instances/<instance-id>/daemon.status.json` | `/home/<user>/.claude/daemon.status.json` |

`<instance-id>` is the `AGENT_INSTANCE_ID` described in
[Container Environment](container-environment.md), so a directory here maps to exactly one
container.

These paths hold the background daemon that runs `& <prompt>` jobs and `claude agents`, plus the
live-session registry. Claude Code keys all of it by **PID**: `daemon.lock` records the holder's
pid and elects a single daemon, and `session-state` names each record after the session's pid. PIDs
are namespace-local and every container's `claude` runs as PID 1, so a project-wide mount would make
concurrent agents resolve each other's pids against their own namespace — writing the same
`session-state/1.json`, and displacing each other's daemon through a lock whose recorded pid means
nothing on the other side.

The subtree is removed when the container exits. A container killed outright leaves its directory
behind, and the next launch in that project collects it once both its container is gone *and* it is
older than an hour — a launcher creates its directory before `docker run` starts the container, so
neither check is sufficient alone. The sweep is skipped entirely when Docker is unreachable, since
"no containers are running" and "the daemon is down" are indistinguishable in a container listing.

## Read-only tool mounts

The wrapper's `ops/` directory is mounted at `/opt/agent-wrap/` so the in-container agent can inspect and invoke those files (the base `Dockerfile`, `default-CLAUDE.md`, `dockerfile-agent-guide.md`, `statusline.py`, `telegram-notify.sh`, `validate-dockerfile-agent`, `wl-paste-shim`). The wrapper's Python source is not mounted:

| Host | Container |
| --- | --- |
| `<wrap-dir>/ops` | `/opt/agent-wrap` (read-only) |

## WSLg (conditional)

On WSL2 hosts with WSLg (detected when `/mnt/wslg` is a directory), three additional mounts enable clipboard passthrough. Only these two `/mnt/wslg` sub-paths are mounted — never the whole `/mnt/wslg` tree:

| Host | Container |
| --- | --- |
| `/mnt/wslg/runtime-dir` | `/mnt/wslg/runtime-dir` |
| `/mnt/wslg/.X11-unix` | `/tmp/.X11-unix` |
| `<wrap-dir>/ops/wl-paste-shim` | `/usr/local/bin/wl-paste` (read-only) |

See [WSLg Clipboard](wslg-clipboard.md) for details.

## Declared by the project Dockerfile (conditional)

`# agent-run-args:` reaches `docker run` untouched, mounts included — but the wrapper reads the
mounts back out of it and prepares the host side first, as the host user, for the same reason it
pre-creates its own mount sources: anything Docker has to materialize itself lands as `root:root`.

| Declaration | Prepared on the host |
| --- | --- |
| `-v /srv/data:/data` | `/srv/data`, created if missing |
| `-v ./scratch:/scratch` | `<project>/scratch` — Docker resolves a relative source against the launch directory, which is the project |
| `-v /srv/models:/models:ro` | nothing: a *missing* read-only source fails the launch instead of mounting an empty directory |
| `-v /workspace/node_modules` | `<project>/node_modules` — the anonymous volume's mountpoint, which Docker would otherwise create as root inside your project |

Both `-v`/`--volume` and `--mount type=bind` are read, along with `--tmpfs`; any target nested under
`/workspace` gets its mountpoint pre-created whatever the mount's kind. A missing writable source is
created as a *directory*, matching what Docker itself would do, so a single-file bind mount has to
exist on the host beforehand. `~` and `$VAR` are never expanded — no shell is involved — so use
absolute or `./`-relative paths. See
[Docker Sandboxing](docker-sandboxing.md#recognized-directives).

## Deliberately not mounted

These paths are container-local and vanish on exit. That is intentional:

| Path | Contents | Why not |
| --- | --- | --- |
| `~/.cache/claude-latest-version` | version-check cache | cheap to rebuild; nothing to preserve |
| `~/.npm` | npm cache | project dependencies belong in the project's own manifest |
| `~/.config`, `~/.local/share/applications` | mimeapps, URL handler | desktop integration the container never uses |
| `/tmp` generally | node compile cache, X11 socket | ephemeral by design |

Everything under `/home/<user>/.claude` persists already — via the global `.claude_config/.claude`
mount if it is not one of the per-project overlays above.

## Notes

- The container runs as your host user (`$(id -u):$(id -g)`) with `HOME` pointing at `/home/<user>` (default `/home/ubuntu`). Under rootless Docker this is `--user 0:0` instead — the daemon maps container-root to the host user, so bind-mounted files are still written as the host user. See [Docker Sandboxing](docker-sandboxing.md#build-args).
- A `.claude/` directory is auto-created in each project and git-ignored.
