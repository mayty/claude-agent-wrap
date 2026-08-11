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
| `$(pwd)/.claude/session-state` | `/home/<user>/.claude/sessions` |

The following subdirectories and files are mapped to the same path on both sides (e.g., `$(pwd)/.claude/daemon` → `/home/<user>/.claude/daemon`):

- **Directories**: `daemon`, `jobs`, `plans`, `todos`, `tasks`, `shell-snapshots`, `session-env`, `file-history`, `paste-cache`, `image-cache`
- **Files**: `daemon.lock`, `daemon.log`, `daemon.status.json`, `history.jsonl`

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
agent unable to write anything else under `~/.cache`. A `Dockerfile.agent` that sets a custom
`# agent-user:` must pre-create that path itself — see
[Docker Sandboxing](docker-sandboxing.md#recognized-directives).

Neither mount is garbage-collected. Per-session directories accumulate under
`$(pwd)/.claude/claude-tmp/`; prune them yourself if they grow.

On launch the wrapper also creates `$(pwd)/.claude/litellm-logs` as a **symlink** (not a bind mount) pointing at this project's slice of the LiteLLM request logs under `<wrap-dir>/litellm-logs/<project_hash>/`, so the `agent logs` viewer reads them through the project's own `.claude/`. That store is shared across projects *and* providers — every provider's sidecar mounts it, and records land under `<project_hash>/<provider>/`.

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
