<!-- This file has been edited with the assistance of an AI tool. -->
# Global instructions

## Environment

You are running inside a Docker container managed by the `agent-wrap` tooling. Filesystem changes inside the container are discarded when it exits — only `/workspace` and the Claude home directory persist.

Within the Claude home directory (`$HOME/.claude/`), most paths are shared across all projects. A specific set are overlaid with per-project mounts from `$(pwd)/.claude/<subdir>/` on the host — `sessions`, `memory`, `session-state`, `daemon`, `jobs`, `plans`, `todos`, `tasks`, `shell-snapshots`, `session-env`, `file-history`, `paste-cache`, `image-cache`, and the files `daemon.lock`, `daemon.log`, `daemon.status.json`, `history.jsonl`. Content you write under those paths is visible only within this project.

The wrapper's operational files are mounted read-only at `/opt/agent-wrap/`: `Dockerfile` (the base image), `default-CLAUDE.md` (this file), `dockerfile-agent-guide.md`, `statusline.py`, `telegram-notify.sh`, `validate-dockerfile-agent`, and `wl-paste-shim`. Consult those when guidance below is ambiguous. The wrapper's Python source is **not** mounted — it stays on the host.

**Important:** You always run as a non-root user and are never granted `sudo` access. Do not attempt to use `sudo` or assume root privileges. If a task requires elevated permissions, instruct the user to add the necessary `RUN` steps to their `Dockerfile.agent` instead.

**Clipboard:** on WSL2 + WSLg hosts the wrapper auto-mounts display sockets and forwards `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`. Claude Code's `Ctrl+V` for Windows-clipboard images works out of the box — do not add clipboard packages or WSLg mounts to a `Dockerfile.agent`.

## Installing dependencies

Do **not** install dependencies ad-hoc inside the running container (`apt-get install`, `pip install`, `npm install -g`, etc.). Changes are discarded when the session ends.

Instead:

- **If `Dockerfile.agent` exists:** edit it — see [Per-project customization](#per-project-customization) below, then tell the user to run `agent rebuild`.
- **If there is no `Dockerfile.agent`:** create one — see [Per-project customization](#per-project-customization) below.

Project-level dependencies that belong in the project's own manifest (`package.json`, `requirements.txt`, `go.mod`, etc.) can be installed normally — those live in `/workspace` and persist.

## Per-project customization

Read [dockerfile-agent-guide.md](/opt/agent-wrap/dockerfile-agent-guide.md) when you or the user need extra tools (language runtimes, system libraries, custom ports/devices, or `docker run` flags), or when you need a tool that isn't available in the current image to work efficiently.

## AI attribution

Whenever you create or edit a file, ensure one of these lines appears at the very top — match the file's comment syntax:

- **Create**: `This file has been created with the assistance of an AI tool.`
- **Edit**: `This file has been edited with the assistance of an AI tool.`

Leave existing attribution lines alone — do not replace "created" with "edited". For formats that disallow comments (JSON), skip it.
