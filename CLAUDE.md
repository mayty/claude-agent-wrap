<!-- This file has been edited with the assistance of an AI tool. -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository provides a Docker-based wrapper for running Claude Code CLI through AWS Bedrock. It packages Claude Code into a container and provides bash functions for easy invocation.

## Architecture

- **Dockerfile**: Builds an Ubuntu 24.04-based image with Node.js 24.x and Claude Code CLI installed globally. Also bakes in `hadolint` and `crane` for use by the in-container validator, plus `wl-clipboard`, `xclip`, and `imagemagick` so Claude Code's `Ctrl+V` can paste images from the WSLg-bridged Windows clipboard (ImageMagick is used by the `wl-paste` shim to convert WSLg's BMP-only clipboard images to PNG on the fly). Configured to use AWS Bedrock for Claude API access.
- **agent-wrap.bashrc**: Provides bash functions to be sourced in your shell:
  - `agent([--base] [args...])`: Runs Claude Code in Docker with proper volume mounts and credentials. With `--base`, ignores any `Dockerfile.agent` in the current directory and launches the base `claude-agent` image instead (no project-specific `EXPOSE`, `agent-user`, or `agent-run-args` are applied). The flag is consumed by `agent()` itself; remaining args are forwarded to the in-container `claude` CLI.
  - `rebuild_agent([--full])`: Rebuilds the resolved image with `--no-cache`. With `--full`, rebuilds the base `claude-agent` image first, then the project image. Without `--full` in a project whose `Dockerfile.agent` uses `FROM claude-agent` and the base is missing, fails fast with a hint pointing at `--full`. Without `--full` in a project whose `Dockerfile.agent` inherits from a non-`claude-agent` base, prints a one-line migration suggestion but builds normally.
  - `create_custom_agent()`: Scaffolds a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory.
  - `agent_usage()`: Aggregates token usage and estimated cost across every directory the user has launched `agent` in. Reads the project registry at `<wrap-dir>/.agent-launches/projects.txt` (a flat list of absolute paths, appended to by `agent()` on each invocation) and walks each project's `.claude/sessions/*.jsonl` files. Prints an aligned table sorted by cost descending plus per-model and per-day breakdowns (the per-day section defaults to the last 30 calendar days in host-local time; pass `--days N` through to widen, or `--days 0` to show every active day). Runs on the host (only the host can see every project's session data — each container only mounts one project at a time). Implemented as a thin bash wrapper around `agent_usage.py`.
- **agent_usage.py**: Python script (stdlib only) invoked by the `agent_usage` shell function. Streams session JSONL files, sums `message.usage` token counts grouped by `message.model` and by host-local calendar date, and converts to USD using AWS Bedrock's published pricing — fetched on first run from `aws.amazon.com/bedrock/pricing/` (which is joined with `b0.p.awsstatic.com/.../bedrockfoundationmodels.json` to resolve the page's opaque `priceOf!...` placeholders into real numbers) and cached at `<wrap-dir>/.agent-launches/pricing.json` for 7 days. Region defaults to "US East (N. Virginia)" to match the wrapper's `AWS_REGION=us-east-1`; `--region` and `--refresh` flags can override, and `--days N` controls the per-day window (default 30, `0` = all). Unknown models render their cost as `?` rather than zero. Lives next to `statusline.py`; runs on the host, not mounted into the container.
- **validate-dockerfile-agent**: Shell script mounted read-only into every container at `/opt/agent-wrap/validate-dockerfile-agent`. Validates a project's `Dockerfile.agent` before the agent prompts the user to rebuild: runs hadolint, checks wrapper-contract directives, and confirms the expected in-container user exists — catching "build succeeds, launch fails" scenarios at write time. When the `Dockerfile.agent` uses `FROM claude-agent`, the validator hardcodes that the base provides user `ubuntu`; for other bases it uses `crane` to probe the image's `/etc/passwd` from the registry (no Docker daemon).
- **statusline.py**: Python script mounted read-only at `/opt/agent-wrap/statusline.py` and wired into the user's `settings.json` as the `statusLine` command on first launch. Renders a two-row status line (model/effort/cost on row 1, context-usage %/update-available notice on row 2). If the user removes the `statusLine` key from their `settings.json`, the wrapper re-injects it on the next launch — to customize, redefine the key instead of deleting it.
- **telegram-notify.sh**: Bash script mounted read-only at `/opt/agent-wrap/telegram-notify.sh` and invoked by `PermissionRequest`, `Stop`, and `StopFailure` hooks when Telegram credentials are present in `~/claude_keys.json`. Sends a Telegram message when Claude asks for permission, finishes responding, or hits an API error. Hook entries are idempotently injected into `settings.json` by `_agent_ensure_telegram_hooks()` on each `agent()` launch when creds are configured.
- **md_to_html.js**: Node script mounted read-only at `/opt/agent-wrap/md_to_html.js` and invoked by `telegram-notify.sh` to convert Markdown into Telegram's subset of HTML (bold/italic/code/links/strikethrough + `<pre>` with optional language tag for code blocks).
- **wl-paste-shim**: Bash shim mounted read-only at `/usr/local/bin/wl-paste` (only when `/mnt/wslg` exists on the host) so it shadows the real `/usr/bin/wl-paste` via PATH order. WSLg advertises Windows clipboard images as `image/bmp` only, but Claude Code's `Ctrl+V` paste handler asks for `image/png` and doesn't fall back. The shim intercepts `--list-types` (advertises `image/png` when only BMP is on clipboard) and `--type image/png` (fetches BMP and pipes through `convert bmp:- png:-`), and falls through to the real binary for everything else.

## Key Configuration

### Environment Variables (set by `agent()` at `docker run` time)
- `CLAUDE_CODE_USE_BEDROCK=1`: Enables AWS Bedrock integration
- `AWS_REGION=us-east-1`: Default AWS region
- `DISABLE_AUTOUPDATER=1`: Disables the Claude Code in-container auto-updater

These are injected via `-e` on each launch rather than baked into the image so users can override them (e.g., point at a different region) without rebuilding.

When `/mnt/wslg` exists on the host (WSL2 + WSLg), `agent()` additionally forwards `DISPLAY` and `WAYLAND_DISPLAY` from the host shell and sets `XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir` so Wayland/X11 clipboard clients in the container reach WSLg's sockets. On non-WSL hosts the block is a no-op.

### `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent()` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

Use this when running multiple WSL2 distros that each run their own `dockerd`. The two daemons share one kernel and fight over `iptables-legacy` rules — the second daemon's startup flips the legacy `FORWARD` policy to `DROP`, stranding the first distro's containers (parent shell stays online; only forwarded/routed traffic dies). `--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces (e.g. `eth6`), not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. In-container services should bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).

### `CLAUDE_AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent()` and `rebuild_agent()` perform a best-effort upstream check on every invocation: `git fetch` against the wrap-dir's tracking branch, and if `HEAD` is behind, prompt the user `Update agent-wrap now? [y/N]`. On accept, the wrapper runs `agent-wrap_update`, re-sources `agent-wrap.bashrc` in the parent shell, and returns without launching/rebuilding — the user re-runs the original command afterwards (per the spec, an accepted update intentionally does not chain into the original action). Decline with `n`/Enter and the original command runs as usual.

Set `CLAUDE_AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. Any unexpected condition — non-git wrap-dir, detached HEAD, `git fetch` failure or 10s timeout — also silently skips the check, so a flaky network never blocks a launch.

Other wrap functions (`agent_usage`, `create_custom_agent`, `agent-wrap_update` itself) do not perform the check.

### Volume Mounts (in agent function)

The wrapper mirrors a minimal `$HOME` layout into the container at `/home/<agent-user>` (default `ubuntu`). `HOME` is set to that path so Claude Code finds its config in the usual spot.

- `<wrap-dir>/.claude_config/.claude.json` → `/home/<user>/.claude.json` (global Claude config file)
- `<wrap-dir>/.claude_config/.claude/` → `/home/<user>/.claude/` (global Claude directory: `CLAUDE.md`, `settings.json`, etc.)
- `$(pwd)` → `/workspace` (project files)
- `$(pwd)/.claude/sessions/` → `/home/<user>/.claude/projects/-workspace/` (per-project session history, overlays on top of the global `.claude` mount)
- `$(pwd)/.claude/{plans,todos,tasks,shell-snapshots,session-env,file-history,paste-cache}/` → `/home/<user>/.claude/<same>/` (per-project overlays for plan-mode files, TodoWrite/TaskCreate state, task definitions, shell snapshots, session env, file-edit history, and paste cache)
- `<wrap-dir>/Dockerfile` → `/opt/agent-wrap/Dockerfile` (read-only; lets the agent inspect its own base image)
- `<wrap-dir>/agent-wrap.bashrc` → `/opt/agent-wrap/agent-wrap.bashrc` (read-only; lets the agent inspect the launcher contract)
- `<wrap-dir>/validate-dockerfile-agent` → `/opt/agent-wrap/validate-dockerfile-agent` (read-only; validator the agent runs before prompting rebuild)
- `<wrap-dir>/statusline.py` → `/opt/agent-wrap/statusline.py` (read-only; the default Claude Code status-line script, invoked via `settings.json`)
- `<wrap-dir>/telegram-notify.sh` → `/opt/agent-wrap/telegram-notify.sh` (read-only; Telegram notification script invoked by hooks)
- `<wrap-dir>/md_to_html.js` → `/opt/agent-wrap/md_to_html.js` (read-only; Markdown→Telegram-HTML converter used by `telegram-notify.sh`)

When the host is WSL2 with WSLg (i.e. `/mnt/wslg` exists), `agent()` also adds:
- `/mnt/wslg` → `/mnt/wslg` (Wayland + Pulse sockets, `runtime-dir/wayland-0`)
- `/mnt/wslg/.X11-unix` → `/tmp/.X11-unix` (XWayland socket, the conventional X11 path)
- `<wrap-dir>/wl-paste-shim` → `/usr/local/bin/wl-paste` (read-only; shadows the real binary so callers asking for `image/png` get on-the-fly BMP→PNG conversion of WSLg-surfaced clipboard images)

These are gated on `[ -d /mnt/wslg ]` so they have no effect on macOS or native Linux hosts.

### Authentication
The `agent()` function expects credentials in `~/claude_keys.json` with the structure:
```json
{
  "ServiceSpecificCredential": {
    "ServiceCredentialSecret": "your-aws-bearer-token"
  },
  "TelegramBotToken": "123456:ABC-DEF...",
  "TelegramChatId": "123456789"
}
```

`TelegramBotToken` and `TelegramChatId` are optional. If both are present, the wrapper forwards them as env vars into the container and injects `PermissionRequest` / `Stop` / `StopFailure` hooks into `settings.json` so Telegram notifications fire. If either is missing, no hooks are injected and no env vars are set (the script would no-op anyway).

## Common Commands

### Build the Docker image
```bash
source agent-wrap.bashrc
rebuild_agent
```

### Run Claude Code
```bash
source agent-wrap.bashrc
agent [arguments]
```

### Project Structure
- `.claude/`: Per-project Claude sessions (git-ignored)
- `.claude_config/`: Global Claude configuration files (git-ignored)
- `.gitignore`: Excludes `.claude_config` from version control

## Per-project customization

A project can provide its own `Dockerfile.agent` at its root to layer project-specific tooling on top of the base image. The recommended template is `FROM claude-agent` — the base provides Node, the Claude CLI, hadolint, crane, clipboard tooling, `WORKDIR /workspace`, and `ENTRYPOINT ["claude"]`, so the project file only needs to add its own `RUN` steps. `create_custom_agent` writes that stub. Existing files that inherit from `ubuntu:24.04` (or any other base) keep working — `rebuild_agent` prints a one-line migration suggestion but does not change behavior.

The file must start with a `# agent-name: <name>` comment; the built image is tagged `claude-agent-<name>`. Additional directives are recognized:

### `# agent-user: <username>`

Overrides the in-container username (default `ubuntu`). This changes where the wrapper expects `$HOME` to be inside the container — the global config and project session mounts are rerouted to `/home/<username>/.claude.json` and `/home/<username>/.claude/`. Use this only when the base image has been customized to run as a different user.

### `# agent-run-args: <flags>`

Extra flags passed through verbatim to `docker run`. Multiple lines are allowed; each line is whitespace-split into tokens (no shell quoting — args containing spaces cannot be expressed). Example:

```dockerfile
# agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN
```

Security note: these flags are pass-through to `docker run`, so a `Dockerfile.agent` can request `--privileged`, host mounts, etc. Review comment lines as well as `RUN` instructions when auditing a third-party `Dockerfile.agent`. In particular, mounting the host Docker socket (`-v /var/run/docker.sock:/var/run/docker.sock`) gives the container host-root-equivalent access and should never be added by an agent on its own initiative — only when the user has explicitly asked for it and acknowledged the risk. Same goes for other escape-hatch flags (`--privileged`, `--pid=host`, `--network=host`, bind-mounts of `/`, `~/.ssh`, cloud credential dirs, etc.).

### `HOST_UID` / `HOST_GID` build args

`rebuild_agent` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. A `Dockerfile.agent` that needs host-UID awareness at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. Projects that don't use these args are unaffected — the base `Dockerfile` declares them as no-ops to silence Docker's unused-build-arg warning.

Because the baked-in UID differs per host user, each user on a shared host builds their own image variant under the same tag.

## Keeping `default-CLAUDE.md` in sync

`default-CLAUDE.md` is copied into every consumer project's `.claude_config/.claude/CLAUDE.md` on first `agent` run and is how agents running in *other* projects learn about this wrapper's runtime contract (directives, mounts, installation rules, etc.).

**Whenever you change wrapper behavior that a consumer agent needs to know about, update `default-CLAUDE.md` in the same change.** This includes:

- Adding, renaming, or removing a `Dockerfile.agent` directive (e.g., `# agent-user:`, `# agent-run-args:`, `EXPOSE` handling).
- Changing mount paths, `HOME`, or other environment assumptions an agent might rely on.
- Changing the rules around installing dependencies, sudo/root access, or the working directory.
- Changing the set of files/directories that persist across container restarts.

What does **not** require a `default-CLAUDE.md` update:

- Internal refactors of `agent-wrap.bashrc` that don't change observable behavior.
- Changes to this repo's own `CLAUDE.md` (which governs editing *this* repo, not consumer projects).
- Host-side changes invisible inside the container (e.g., how `.claude_config/` is laid out on disk).

`agent-wrap_update` handles propagation: if a user's copy matches the old default, it is replaced automatically; if it's been customized, they get a diff-and-merge prompt. So updating `default-CLAUDE.md` is the only action required on the wrapper side — consumer projects pick up the change on their next `agent-wrap_update`.

## Notes

- The Docker container runs as the current user (`$(id -u):$(id -g)`) to avoid permission issues
- `HOME` inside the container is set to `/home/<agent-user>` (default `/home/ubuntu`); global Claude state lives under that path and is mounted from `<wrap-dir>/.claude_config/`
- The project `.claude` directory is automatically created and git-ignored by the wrapper script
