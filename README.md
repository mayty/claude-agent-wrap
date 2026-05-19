<!-- This file has been edited with the assistance of an AI tool. -->
# claude-agent-wrap

A Docker-based wrapper for running the [Claude Code](https://github.com/anthropics/claude-code) CLI against AWS Bedrock. It packages Claude Code into a reproducible container image and exposes two bash functions — `agent` and `rebuild_agent` — that handle volume mounts, credentials, and per-project image customization.

## Why

Running Claude Code in a container isolates the tool from your host system, pins its dependencies, and lets each project override the base image with its own `Dockerfile.agent` to supply project-specific runtime requirements. The wrapper also routes the CLI through AWS Bedrock instead of the Anthropic API, so auth is an AWS bearer token rather than an Anthropic API key.

## Requirements

- Docker
- `jq` (used by the wrapper to read credentials)
- An AWS Bedrock bearer token, stored in `~/claude_keys.json`:
  ```json
  {
    "ServiceSpecificCredential": {
      "ServiceCredentialSecret": "your-aws-bearer-token"
    }
  }
  ```
- (Optional) Telegram credentials for permission-request and stop notifications, added to the same `~/claude_keys.json` (see [Telegram notifications](#telegram-notifications) for how to obtain them):
  ```json
  {
    "ServiceSpecificCredential": {
      "ServiceCredentialSecret": "your-aws-bearer-token"
    },
    "TelegramBotToken": "your-telegram-bot-token",
    "TelegramChatId": "your-telegram-chat-id"
  }
  ```

## Setup

Source the wrapper in your shell (add it to `~/.bashrc` or `~/.zshrc` to make it permanent):

```bash
source /path/to/claude-agent-wrap/agent-wrap.bashrc
```

Build the base image once:

```bash
rebuild_agent
```

This creates a `claude-agent` image tagged with your host UID/GID so the container can write to mounted directories without permission issues.

## Usage

From any project directory, run:

```bash
agent [claude-code-args...]
```

The wrapper mounts:

| Host | Container | Purpose |
| --- | --- | --- |
| `$(pwd)` | `/workspace` | Project files |
| `<wrap-dir>/.claude_config/.claude.json` | `/home/<user>/.claude.json` | Global Claude config file |
| `<wrap-dir>/.claude_config/.claude` | `/home/<user>/.claude` | Global Claude directory (`CLAUDE.md`, `settings.json`, caches, etc.) |
| `$(pwd)/.claude/sessions` | `/home/<user>/.claude/projects/-workspace` | Per-project session history (overlays the global `.claude` mount) |
| `$(pwd)/.claude/{plans,todos,tasks,shell-snapshots,session-env,file-history,paste-cache}` | `/home/<user>/.claude/<same>` | Per-project state overlays (plans, todos, tasks, shell snapshots, session env, file history, paste cache) |

The wrapper also bind-mounts its own source files read-only under `/opt/agent-wrap/` so the in-container agent can inspect and invoke them (the validator, status line, Telegram script, etc.).

The container runs as your host user (`$(id -u):$(id -g)`) with `HOME` pointing at `/home/<user>` (default `/home/ubuntu`). A `.claude/` directory is auto-created in each project and git-ignored.

## Telegram notifications

Claude Code can send you a Telegram message when it asks for permission to run a tool, finishes a response, or hits an API error. Useful if you step away mid-session.

### Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and note the bot token.
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.
3. Add both to `~/claude_keys.json`:
   ```json
   {
     "ServiceSpecificCredential": {
       "ServiceCredentialSecret": "your-aws-bearer-token"
     },
     "TelegramBotToken": "11111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
     "TelegramChatId": "22222222"
   }
   ```

On the next `agent` launch, the wrapper idempotently injects three hook entries into `<wrap-dir>/.claude_config/.claude/settings.json` and forwards the credentials as env vars into the container. No `rebuild_agent` needed — the script and its Markdown→HTML converter are bind-mounted live.

### How it works

- **`PermissionRequest` hook** — fires when Claude asks to use a tool. Sends a tool-specific message (shell command with syntax highlighting for `Bash`, file paths for `Write`/`Edit`/`Read`, etc.).
- **`Stop` hook** — fires when Claude finishes its response. Sends the last assistant text (non-thinking content only).
- **`StopFailure` hook** — fires when the turn ends on an API error.

The hooks only fire if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the container environment. The script returns `{}` and exits 0 on every path, so it never blocks Claude — even if the Telegram API is unreachable.

## Per-project customization

To layer project-specific tooling on top of the base image, drop a `Dockerfile.agent` at the root of your project. The simplest way is to scaffold a thin stub that inherits from the base image:

```bash
create_custom_agent   # writes a ./Dockerfile.agent with `FROM claude-agent`
```

The generated stub looks like:

```dockerfile
# agent-name: <derived-from-dirname>
FROM claude-agent

# Add project-specific RUN steps here.
```

Add your project-specific `RUN` steps below the `FROM`, then rebuild from inside that project:

```bash
rebuild_agent
```

The resulting image is tagged `claude-agent-<name>` and `agent` will pick it up automatically whenever you invoke it from that directory. The base toolchain (Node, Claude CLI, hadolint, crane, clipboard tooling, `WORKDIR /workspace`, `ENTRYPOINT ["claude"]`) is inherited from `claude-agent`, so there's no need to redeclare it.

If the base `claude-agent` image hasn't been built yet on this host, run `rebuild_agent --full` once — it builds the base first, then the project image.

**Backwards compatibility:** existing projects whose `Dockerfile.agent` starts with `FROM ubuntu:24.04` (or any other non-`claude-agent` base) keep working as-is; `rebuild_agent` will print a one-line note suggesting migration but does not change behavior. To migrate, replace the body with `FROM claude-agent` plus your project-specific additions.

### Recognized directives

`Dockerfile.agent` supports a few wrapper-specific comment directives in addition to normal Dockerfile syntax:

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`. Must match `[a-z0-9_.-]+` (Docker image names are lowercase).
- **`# agent-user: <username>`** — sets the in-container username (default `ubuntu`). The wrapper reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`. Only useful if the base image has been customized to run as a different user.
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; tokens are whitespace-split (no shell quoting). Example:
  ```dockerfile
  # agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN
  ```
- **`EXPOSE <port>`** — any standard `EXPOSE` directives cause the wrapper to publish those ports on `127.0.0.1`.

### Build args

`rebuild_agent` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. A `Dockerfile.agent` that needs these at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. Because the baked-in UID differs per host user, each user on a shared host builds their own image variant under the same tag.

### Security note

`agent-run-args` is a pass-through to `docker run`, so a third-party `Dockerfile.agent` can request `--privileged`, host bind mounts, etc. Audit comment lines as well as `RUN` instructions before building someone else's agent image.

## Functions reference

| Function | Purpose |
| --- | --- |
| `agent [args...]` | Run Claude Code in a container against the resolved image for the current directory. |
| `rebuild_agent [--full]` | Rebuild the resolved image with `--no-cache`, passing `HOST_UID`/`HOST_GID`. With `--full`, rebuild the base `claude-agent` image first, then the project image. |
| `create_custom_agent` | Scaffold a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory. |
| `agent-wrap_update` | Pull the latest wrapper source; if `default-CLAUDE.md` changed, replace the user's copy when unmodified or warn when customized. |

## Environment

The `agent()` function injects these env vars on each `docker run` (not baked into the image, so overriding them doesn't require a rebuild):

- `CLAUDE_CODE_USE_BEDROCK=1` — routes Claude Code through AWS Bedrock.
- `AWS_REGION=us-east-1` — default Bedrock region.
- `DISABLE_AUTOUPDATER=1` — disables the Claude Code in-container auto-updater.

The bearer token is injected at runtime as `AWS_BEARER_TOKEN_BEDROCK`, read from `~/claude_keys.json`.

If both `TelegramBotToken` and `TelegramChatId` are present in `~/claude_keys.json`, they are forwarded into the container as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and consumed by the notification hooks. Missing either one skips the forwarding entirely.

### `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

Use this when you run multiple WSL2 distros that each have their own `dockerd`. All WSL2 distros share a single Linux kernel, so the two daemons fight over the kernel's iptables tables — specifically, the second daemon to start installs Docker's standard ruleset on `iptables-legacy`, which flips the legacy `FORWARD` chain policy from `ACCEPT` to `DROP`. Reply traffic to the first distro's existing containers then gets dropped before it reaches `docker0`. Symptom: parent shell stays online, but containers lose all outbound TCP (DNS UDP still works); recovery requires `wsl --shutdown`. Relaunching the container does not help, because the broken state is upstream of `docker0`.

`--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and the FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces, not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. Make in-container services bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).

## Layout

```
.
├── Dockerfile                   # Base image: Ubuntu 24.04 + Node 24 + Claude Code CLI + hadolint + crane
├── agent-wrap.bashrc            # Shell functions: agent, rebuild_agent, create_custom_agent, agent-wrap_update
├── validate-dockerfile-agent    # Pre-build validator (hadolint, contract checks, crane user probe)
├── statusline.py                # Status bar script (model/cost, context %/update notice)
├── telegram-notify.sh           # PermissionRequest / Stop / StopFailure Telegram notifications
├── md_to_html.js                # Markdown → Telegram-HTML converter used by telegram-notify.sh
├── default-CLAUDE.md            # Default instructions (copied into consumer projects' global config)
├── CLAUDE.md                    # Repo-level guidance (for editing this project)
├── README.md
└── .claude_config/              # Global Claude config (git-ignored, auto-created)
```
