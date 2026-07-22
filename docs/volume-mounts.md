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

On launch the wrapper also creates `$(pwd)/.claude/litellm-logs` as a **symlink** (not a bind mount) pointing at this project's slice of the shared LiteLLM request logs under `<wrap-dir>/litellm-logs/<project_hash>/`, so the `agent logs` viewer reads them through the project's own `.claude/`.

## Read-only tool mounts

The wrapper's own source files are mounted at `/opt/agent-wrap/` so the in-container agent can inspect and invoke them (the validator, status line, Telegram script, etc.):

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

## Notes

- The container runs as your host user (`$(id -u):$(id -g)`) with `HOME` pointing at `/home/<user>` (default `/home/ubuntu`). Under rootless Docker this is `--user 0:0` instead — the daemon maps container-root to the host user, so bind-mounted files are still written as the host user. See [Docker Sandboxing](docker-sandboxing.md#build-args).
- A `.claude/` directory is auto-created in each project and git-ignored.
