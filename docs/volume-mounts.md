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
| `$(pwd)/.claude/session-state` | `/home/<user>/.claude/sessions` |

The following subdirectories and files are mapped to the same path on both sides (e.g., `$(pwd)/.claude/daemon` → `/home/<user>/.claude/daemon`):

- **Directories**: `daemon`, `jobs`, `plans`, `todos`, `tasks`, `shell-snapshots`, `session-env`, `file-history`, `paste-cache`, `image-cache`
- **Files**: `daemon.lock`, `daemon.log`, `daemon.status.json`, `history.jsonl`

## Read-only tool mounts

The wrapper's own source files are mounted at `/opt/agent-wrap/` so the in-container agent can inspect and invoke them (the validator, status line, Telegram script, etc.):

| Host | Container |
| --- | --- |
| `<wrap-dir>/ops` | `/opt/agent-wrap` (read-only) |

## WSLg (conditional)

On WSL2 hosts with WSLg (detected when `/mnt/wslg` is a directory), three additional mounts enable clipboard passthrough:

| Host | Container |
| --- | --- |
| `/mnt/wslg` | `/mnt/wslg` |
| `/mnt/wslg/.X11-unix` | `/tmp/.X11-unix` |
| `<wrap-dir>/ops/wl-paste-shim` | `/usr/local/bin/wl-paste` (read-only) |

See [WSLg Clipboard](wslg-clipboard.md) for details.

## Notes

- The container runs as your host user (`$(id -u):$(id -g)`) with `HOME` pointing at `/home/<user>` (default `/home/ubuntu`).
- A `.claude/` directory is auto-created in each project and git-ignored.
