<!-- This file has been edited with the assistance of an AI tool. -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository provides a Docker-based wrapper for running Claude Code CLI through AWS Bedrock. It packages Claude Code into a container and provides bash functions for easy invocation.

## Architecture

- **Dockerfile**: Builds an Ubuntu 24.04-based image with Node.js 24.x and Claude Code CLI installed globally. Also bakes in `hadolint` and `crane` for use by the in-container validator. Configured to use AWS Bedrock for Claude API access.
- **agent-wrap.bashrc**: Provides bash functions to be sourced in your shell:
  - `agent()`: Runs Claude Code in Docker with proper volume mounts and credentials
  - `rebuild_agent()`: Rebuilds the Docker image with --no-cache
- **validate-dockerfile-agent**: Shell script mounted read-only into every container at `/opt/agent-wrap/validate-dockerfile-agent`. Validates a project's `Dockerfile.agent` before the agent prompts the user to rebuild: runs hadolint, checks wrapper-contract directives, and uses `crane` to probe the base image's `/etc/passwd` from the registry (no Docker daemon) to confirm the expected in-container user actually exists — catching "build succeeds, launch fails" scenarios at write time.
- **statusline.py**: Python script mounted read-only at `/opt/agent-wrap/statusline.py` and wired into the user's `settings.json` as the `statusLine` command on first launch. Renders a two-row status line (model/effort/cost on row 1, context-usage %/update-available notice on row 2). If the user removes the `statusLine` key from their `settings.json`, the wrapper re-injects it on the next launch — to customize, redefine the key instead of deleting it.
- **telegram-notify.sh**: Bash script mounted read-only at `/opt/agent-wrap/telegram-notify.sh` and invoked by `PermissionRequest`, `Stop`, and `StopFailure` hooks when Telegram credentials are present in `~/claude_keys.json`. Sends a Telegram message when Claude asks for permission, finishes responding, or hits an API error. Hook entries are idempotently injected into `settings.json` by `_agent_ensure_telegram_hooks()` on each `agent()` launch when creds are configured.
- **md_to_html.js**: Node script mounted read-only at `/opt/agent-wrap/md_to_html.js` and invoked by `telegram-notify.sh` to convert Markdown into Telegram's subset of HTML (bold/italic/code/links/strikethrough + `<pre>` with optional language tag for code blocks).

## Key Configuration

### Environment Variables (set by `agent()` at `docker run` time)
- `CLAUDE_CODE_USE_BEDROCK=1`: Enables AWS Bedrock integration
- `AWS_REGION=us-east-1`: Default AWS region
- `DISABLE_AUTOUPDATER=1`: Disables the Claude Code in-container auto-updater

These are injected via `-e` on each launch rather than baked into the image so users can override them (e.g., point at a different region) without rebuilding.

### Volume Mounts (in agent function)

The wrapper mirrors a minimal `$HOME` layout into the container at `/home/<agent-user>` (default `ubuntu`). `HOME` is set to that path so Claude Code finds its config in the usual spot.

- `<wrap-dir>/.claude_config/.claude.json` → `/home/<user>/.claude.json` (global Claude config file)
- `<wrap-dir>/.claude_config/.claude/` → `/home/<user>/.claude/` (global Claude directory: `CLAUDE.md`, `settings.json`, etc.)
- `$(pwd)` → `/workspace` (project files)
- `$(pwd)/.claude/sessions/` → `/home/<user>/.claude/projects/-workspace/` (per-project session history, overlays on top of the global `.claude` mount)
- `<wrap-dir>/Dockerfile` → `/opt/agent-wrap/Dockerfile` (read-only; lets the agent inspect its own base image)
- `<wrap-dir>/agent-wrap.bashrc` → `/opt/agent-wrap/agent-wrap.bashrc` (read-only; lets the agent inspect the launcher contract)
- `<wrap-dir>/validate-dockerfile-agent` → `/opt/agent-wrap/validate-dockerfile-agent` (read-only; validator the agent runs before prompting rebuild)
- `<wrap-dir>/statusline.py` → `/opt/agent-wrap/statusline.py` (read-only; the default Claude Code status-line script, invoked via `settings.json`)
- `<wrap-dir>/telegram-notify.sh` → `/opt/agent-wrap/telegram-notify.sh` (read-only; Telegram notification script invoked by hooks)
- `<wrap-dir>/md_to_html.js` → `/opt/agent-wrap/md_to_html.js` (read-only; Markdown→Telegram-HTML converter used by `telegram-notify.sh`)

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

A project can provide its own `Dockerfile.agent` at its root to override the base image. The file must start with a `# agent-name: <name>` comment; the built image is tagged `claude-agent-<name>`. Additional directives are recognized:

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
