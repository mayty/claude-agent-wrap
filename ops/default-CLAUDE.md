<!-- This file has been edited with the assistance of an AI tool. -->
# Global instructions

## Environment

You are running inside a Docker container managed by the `agent-wrap` tooling. Filesystem changes inside the container are discarded when it exits — only `/workspace`, the Claude home directory, and the two mounts named below persist.

Within the Claude home directory (`$HOME/.claude/`), most paths are shared across all projects. A specific set are overlaid with per-project mounts from `$(pwd)/.claude/<subdir>/` on the host — `sessions`, `memory`, `jobs`, `plans`, `todos`, `tasks`, `shell-snapshots`, `session-env`, `file-history`, `paste-cache`, `image-cache`, and the file `history.jsonl`. Content you write under those paths is visible only within this project.

A smaller set is private to **this container** rather than to the project, mounted from `$(pwd)/.claude/instances/$AGENT_INSTANCE_ID/` on the host — `daemon`, `session-state` (at `$HOME/.claude/sessions`), and the files `daemon.lock`, `daemon.log`, `daemon.status.json`. These hold the background daemon and live-session registry, which Claude Code keys by PID; every container runs its agent as PID 1, so sharing them between concurrent agents would make each read the others' PIDs as its own. The host directory is deleted when this container exits.

Two per-project mounts sit outside that directory: your session scratchpad (under `/tmp/claude-<uid>/`, from host `.claude/claude-tmp/`) and the MCP server logs (`~/.cache/claude-cli-nodejs/-workspace/`, from host `.claude/mcp-logs/`). The scratchpad therefore survives the container — files you leave there are still present if this session is later resumed. Everything else under `/tmp` and `~/.cache` is discarded.

The wrapper's operational files are mounted read-only at `/opt/agent-wrap/`: `Dockerfile` (the base image), `default-CLAUDE.md` (this file), `dockerfile-agent-guide.md`, `statusline.py`, `telegram-notify.sh`, `validate-dockerfile-agent`, and `wl-paste-shim`. Consult those when guidance below is ambiguous. The wrapper's Python source is **not** mounted — it stays on the host.

**Important:** You always run as a non-root user and are never granted `sudo` access. Do not attempt to use `sudo` or assume root privileges. If a task requires elevated permissions, instruct the user to add the necessary `RUN` steps to their `.claude-agent-wrap/Dockerfile` instead.

**Spell checking:** the prompt input's spell checking is wrapper-managed — `hunspell` and its dictionaries are preinstalled and the `spellcheck` block in the global `settings.json` is written on every launch, configured host-side by `AGENT_SPELLCHECK` and `AGENT_SPELLCHECK_LANG`. Do not install a spell checker or edit that block; changing the dictionary list requires a host-side `agent rebuild --full`.

**Clipboard:** on WSL2 + WSLg hosts the wrapper auto-mounts display sockets and forwards `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`. Claude Code's `Ctrl+V` for Windows-clipboard images works out of the box — do not add clipboard packages or WSLg mounts to a `.claude-agent-wrap/Dockerfile`.

## Installing dependencies

Do **not** install dependencies ad-hoc inside the running container (`apt-get install`, `pip install`, `npm install -g`, etc.). Changes are discarded when the session ends.

Instead:

- **If `.claude-agent-wrap/Dockerfile` exists:** edit it — see [Per-project customization](#per-project-customization) below, then tell the user to run `agent rebuild`.
- **If there is no `.claude-agent-wrap/Dockerfile`:** create one — see [Per-project customization](#per-project-customization) below. A project may still carry the deprecated `./Dockerfile.agent`; edit that in place, or offer to migrate it, but never create both — the wrapper refuses to run when both exist.

Project-level dependencies that belong in the project's own manifest (`package.json`, `requirements.txt`, `go.mod`, etc.) can be installed normally — those live in `/workspace` and persist.

## Per-project customization

Per-project wrapper assets live in `.claude-agent-wrap/` at the project root, checked into the project: `Dockerfile` (the project image and its `# agent-*` directives) and the optional `startup.sh` (a host-side script run before each launch, gated by `# agent-enable-startup:`).

Read [dockerfile-agent-guide.md](/opt/agent-wrap/dockerfile-agent-guide.md) when you or the user need extra tools (language runtimes, system libraries, custom ports/devices, or `docker run` flags), when the project needs host-side setup before launch (e.g. a Docker network to attach to), or when you need a tool that isn't available in the current image to work efficiently.

## AI attribution

Whenever you create or edit a file, ensure one of these lines appears at the very top — match the file's comment syntax:

- **Create**: `This file has been created with the assistance of an AI tool.`
- **Edit**: `This file has been edited with the assistance of an AI tool.`

Leave existing attribution lines alone — do not replace "created" with "edited". For formats that disallow comments (JSON), skip it.
