<!-- This file has been edited with the assistance of an AI tool. -->
# claude-agent-wrap

A Docker-based wrapper for the Claude Code CLI that isolates the agent in containers, keeps API credentials out of the agent process (in the default provider), and lets each project customize its environment with a simple `Dockerfile.agent`.

It packages Claude Code into a reproducible container image and exposes bash functions — `agent` and `rebuild_agent` — that handle volume mounts, credentials, and per-project image customization. Model traffic is routed through a provider-selected upstream; the default provider (`litellm-bedrock`) uses a [LiteLLM](https://github.com/BerriAI/litellm) sidecar. See [Providers](#providers) for available options.

## Table of Contents

- [claude-agent-wrap](#claude-agent-wrap)
  - [Table of Contents](#table-of-contents)
  - [Quick Start](#quick-start)
    - [Requirements](#requirements)
    - [Setup](#setup)
    - [Usage](#usage)
  - [Features](#features)
    - [Docker Sandboxing \& Per-Project Customization](#docker-sandboxing--per-project-customization)
      - [Recognized Directives](#recognized-directives)
      - [Build Args](#build-args)
      - [Security Note](#security-note)
    - [Telegram Notifications](#telegram-notifications)
      - [Setup](#setup-1)
      - [How it works](#how-it-works)
    - [Clipboard \& WSLg Support](#clipboard--wslg-support)
  - [Providers](#providers)
    - [Available Providers](#available-providers)
    - [Adding a Provider](#adding-a-provider)
    - [LiteLLM Provider Details](#litellm-provider-details)
  - [Configuration](#configuration)
    - [Wrapper Environment Variables](#wrapper-environment-variables)
      - [`AGENT_PROVIDER` (model-routing backend)](#agent_provider-model-routing-backend)
      - [`AGENT_USE_HOST_NETWORK` (WSL workaround)](#agent_use_host_network-wsl-workaround)
      - [`CLAUDE_AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)](#claude_agent_skip_update_check-auto-update-opt-out)
  - [Reference](#reference)
    - [Volume Mounts](#volume-mounts)
    - [Shell Functions](#shell-functions)
    - [Container Environment Variables](#container-environment-variables)
      - [Always-injected vars](#always-injected-vars)
      - [Provider-injected vars](#provider-injected-vars)
      - [Conditional vars](#conditional-vars)
  - [Project Layout](#project-layout)

## Quick Start

### Requirements

- Docker
- API credentials for your chosen provider, stored in `~/claude_keys.json`. The default provider (`litellm-bedrock`) expects:
  ```json
  {
    "ServiceSpecificCredential": {
      "ServiceCredentialSecret": "your-aws-bearer-token"
    }
  }
  ```
  Other providers have different keys — see their READMEs for the expected format.
- (Optional) Telegram credentials for permission-request and stop notifications, added to the same `~/claude_keys.json` (see [Telegram notifications](#telegram-notifications) for how to obtain them):
  ```json
  {
    ...
    "TelegramBotToken": "your-telegram-bot-token",
    "TelegramChatId": "your-telegram-chat-id"
  }
  ```

### Setup

Source the wrapper in your shell (add it to `~/.bashrc` or `~/.zshrc` to make it permanent):

```bash
source /path/to/claude-agent-wrap/agent-wrap.bashrc
```

Build the base image once:

```bash
rebuild_agent
```

This creates a `claude-agent` image tagged with your host UID/GID so the container can write to mounted directories without permission issues.

### Usage

From any project directory, run:

```bash
agent [claude-code-args...]
```

If a `Dockerfile.agent` exists in the current directory but you want to run against the base `claude-agent` image for this launch (e.g., to bypass a broken project image or compare behavior), pass `--base`:

```bash
agent --base [claude-code-args...]
```

## Features

Running Claude Code in a container isolates the tool from your host system, pins its dependencies, and lets each project override the base image with its own `Dockerfile.agent` to supply project-specific runtime requirements. The wrapper routes the CLI through a provider-specific upstream (Bedrock by default, but pluggable). The default provider keeps the user's API key inside the sidecar so it never enters the agent container; other providers may handle auth differently. See [Providers](#providers).

### Docker Sandboxing & Per-Project Customization

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

#### Recognized Directives

`Dockerfile.agent` supports a few wrapper-specific comment directives in addition to normal Dockerfile syntax:

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`. Must match `[a-z0-9_.-]+` (Docker image names are lowercase).
- **`# agent-user: <username>`** — sets the in-container username (default `ubuntu`). The wrapper reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`. Only useful if the base image has been customized to run as a different user.
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; tokens are whitespace-split (no shell quoting). Example:
  ```dockerfile
  # agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN
  ```
- **`EXPOSE <port>`** — any standard `EXPOSE` directives cause the wrapper to publish those ports on `127.0.0.1`.

#### Build Args

`rebuild_agent` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. A `Dockerfile.agent` that needs these at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. Because the baked-in UID differs per host user, each user on a shared host builds their own image variant under the same tag.

#### Security Note

`agent-run-args` is a pass-through to `docker run`, so a third-party `Dockerfile.agent` can request `--privileged`, host bind mounts, etc. Audit comment lines as well as `RUN` instructions before building someone else's agent image.

### Telegram Notifications

Claude Code can send you a Telegram message when it asks for permission to run a tool, finishes a response, or hits an API error. Useful if you step away mid-session.

#### Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and note the bot token.
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.
3. Add both to `~/claude_keys.json`:
   ```json
   {
     ...
     "TelegramBotToken": "11111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
     "TelegramChatId": "22222222"
   }
   ```

On the next `agent` launch, the wrapper idempotently injects three hook entries into `<wrap-dir>/.claude_config/.claude/settings.json` and forwards the credentials as env vars into the container. No `rebuild_agent` needed — the script and its Markdown→HTML converter are bind-mounted live.

#### How it works

- **`PermissionRequest` hook** — fires when Claude asks to use a tool. Sends a tool-specific message (shell command with syntax highlighting for `Bash`, file paths for `Write`/`Edit`/`Read`, etc.).
- **`Stop` hook** — fires when Claude finishes its response. Sends the last assistant text (non-thinking content only).
- **`StopFailure` hook** — fires when the turn ends on an API error.

The hooks only fire if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set in the container environment. The script returns `{}` and exits 0 on every path, so it never blocks Claude — even if the Telegram API is unreachable.

<a id="clipboard--wslg"></a>
### Clipboard & WSLg Support

On WSL2 hosts with WSLg enabled (detected via `[ -d /mnt/wslg ]`), `agent` automatically wires the container into the host's clipboard and display sockets so Claude Code's `Ctrl+V` paste of Windows-clipboard images works out of the box. Specifically, the wrapper:

- bind-mounts `/mnt/wslg` and `/mnt/wslg/.X11-unix` (the latter at `/tmp/.X11-unix`, the conventional X11 socket path),
- forwards `DISPLAY` and `WAYLAND_DISPLAY` from the host shell and sets `XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir`, and
- bind-mounts `wl-paste-shim` over `/usr/local/bin/wl-paste` so it shadows the real binary via PATH order.

The shim is needed because WSLg advertises Windows-clipboard images as `image/bmp` only, while Claude Code's paste handler asks for `image/png`. The shim intercepts `--list-types` (advertises `image/png` when only BMP is on the clipboard) and `--type image/png` (fetches BMP and pipes through ImageMagick's `convert bmp:- png:-`), and falls through to the real `wl-paste` for everything else.

On macOS or native Linux hosts the entire block is a no-op.

## Providers

The wrapper routes Claude Code through a pluggable provider. Each provider implements the `Provider` ABC (`agent_wrap/providers/base.py`) — four abstract methods (`ensure`, `release`, `get_run_args`, `get_label_args`) — with no assumption about sidecars, proxies, or network topology.

Select a provider via the `AGENT_PROVIDER` environment variable (default: `litellm-bedrock`):

```sh
AGENT_PROVIDER=litellm-dashscope source agent-wrap.bashrc
agent
```

### Available Providers

| Provider | Description | README |
| --- | --- | --- |
| `litellm-bedrock` | AWS Bedrock via LiteLLM sidecar (default) | [README](agent_wrap/providers/litellm_bedrock/README.md) |
| `litellm-dashscope` | Alibaba Cloud DashScope via LiteLLM sidecar | [README](agent_wrap/providers/litellm_dashscope/README.md) |

### Adding a Provider

Adding a new provider: create `agent_wrap/providers/<name>/provider.py` implementing the `Provider` ABC. Providers are auto-discovered — drop in a directory and it shows up without any registry edits. If your provider uses a LiteLLM sidecar, subclass `LiteLLMProvider` from `litellm_common` and also provide a `config.yaml` for the proxy config; non-LiteLLM providers do not need `config.yaml`.

### LiteLLM Provider Details

The default provider (`litellm-bedrock`) uses a shared `agent-wrap-litellm` sidecar container to front the upstream API. Auth, traffic routing, and network topology details are specific to this provider's implementation — see its README:

- [`litellm_bedrock/README.md`](agent_wrap/providers/litellm_bedrock/README.md) — sidecar lifecycle, auth boundary, networking

## Configuration

### Wrapper Environment Variables

These environment variables affect wrapper behavior, not the container's environment.

#### `AGENT_PROVIDER` (model-routing backend)

Selects which provider plugin to use. Each provider lives in `agent_wrap/providers/<name>/provider.py` and implements the `Provider` ABC from `agent_wrap/providers/base.py`. The default is `litellm-bedrock`, preserving historical behavior.

```sh
# Use the default LiteLLM-Bedrock provider (no var needed)
agent

# Or pick a different one — the launcher fails fast and lists available
# providers if the directory doesn't exist.
AGENT_PROVIDER=my-direct-anthropic source agent-wrap.bashrc
agent
```

Providers are auto-discovered by scanning `agent_wrap/providers/*/provider.py` for concrete `Provider` subclasses (`inspect.getmembers()` + `inspect.isabstract()`) — drop in a directory and it shows up in the error message above without any registry edits.

#### `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

Use this when you run multiple WSL2 distros that each have their own `dockerd`. All WSL2 distros share a single Linux kernel, so the two daemons fight over the kernel's iptables tables — specifically, the second daemon to start installs Docker's standard ruleset on `iptables-legacy`, which flips the legacy `FORWARD` chain policy from `ACCEPT` to `DROP`. Reply traffic to the first distro's existing containers then gets dropped before it reaches `docker0`. Symptom: parent shell stays online, but containers lose all outbound TCP (DNS UDP still works); recovery requires `wsl --shutdown`. Relaunching the container does not help, because the broken state is upstream of `docker0`.

`--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and the FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces, not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. Make in-container services bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).
- The flag also extends to any provider sidecar — when set on the **cold-start** launch, the sidecar is launched with `--network host` as well. First-launch-wins: subsequent launches without the flag adapt to the running mode rather than restarting it. To switch a running sidecar's mode, stop it and start the next launch with the desired flag value.

#### `CLAUDE_AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent` and `rebuild_agent` run a best-effort upstream check on every invocation: a `git fetch` against the wrap-dir's tracking branch, then — if `HEAD` is behind — a `Update agent-wrap now? [y/N]` prompt. On `y`, the wrapper runs `agent-wrap_update` and returns without launching the container or rebuilding the image; re-source `agent-wrap.bashrc` and re-run your original command afterwards. On `n` (or Enter), the original command proceeds unchanged.

Set `CLAUDE_AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. The check is also auto-skipped on any error path — non-git wrap-dir, detached HEAD, fetch failure, or 10-second fetch timeout — so a flaky or offline network never blocks a launch.

Other wrap functions (`agent_usage`, `create_custom_agent`, and `agent-wrap_update` itself) do not perform the check.

## Reference

### Volume Mounts

The `agent` command bind-mounts the following paths into the container:

| Host | Container | Purpose |
| --- | --- | --- |
| `$(pwd)` | `/workspace` | Project files |
| `<wrap-dir>/.claude_config/.claude.json` | `/home/<user>/.claude.json` | Global Claude config file |
| `<wrap-dir>/.claude_config/.claude` | `/home/<user>/.claude` | Global Claude directory (`CLAUDE.md`, `settings.json`, caches, etc.) |
| `$(pwd)/.claude/sessions` | `/home/<user>/.claude/projects/-workspace` | Per-project session transcripts (overlays the global `.claude` mount) |
| `$(pwd)/.claude/session-state` | `/home/<user>/.claude/sessions` | Per-project live-session registry (pid, sessionId, cwd, status — distinct from transcripts) |
| `$(pwd)/.claude/{daemon,jobs}` | `/home/<user>/.claude/{daemon,jobs}` | Per-project supervisor/worker roster and bg-job state |
| `$(pwd)/.claude/{daemon.lock,daemon.log,daemon.status.json,history.jsonl}` | `/home/<user>/.claude/<same>` | Per-project daemon lock/log/status files and shell-prompt history |
| `$(pwd)/.claude/{plans,todos,tasks,shell-snapshots,session-env,file-history,paste-cache}` | `/home/<user>/.claude/<same>` | Per-project state overlays (plans, todos, tasks, shell snapshots, session env, file history, paste cache) |
| `/mnt/wslg`, `/mnt/wslg/.X11-unix`, `<wrap-dir>/ops/wl-paste-shim` | `/mnt/wslg`, `/tmp/.X11-unix`, `/usr/local/bin/wl-paste` | WSL2 + WSLg only — Wayland/X11 sockets and the `wl-paste` shim that surfaces Windows-clipboard images as PNG. See [Clipboard / WSLg](#clipboard--wslg). |

The wrapper also bind-mounts its own source files read-only under `/opt/agent-wrap/` so the in-container agent can inspect and invoke them (the validator, status line, Telegram script, etc.).

The container runs as your host user (`$(id -u):$(id -g)`) with `HOME` pointing at `/home/<user>` (default `/home/ubuntu`). A `.claude/` directory is auto-created in each project and git-ignored.

### Shell Functions

| Function | Purpose |
| --- | --- |
| `agent [--base] [args...]` | Run Claude Code in a container against the resolved image for the current directory. With `--base`, ignore any `Dockerfile.agent` in the current directory and launch the base `claude-agent` image instead (project-specific `EXPOSE`, `agent-user`, and `agent-run-args` directives are skipped). On every invocation, performs a best-effort upstream check and prompts to pull if the wrap-dir is behind (see [`CLAUDE_AGENT_SKIP_UPDATE_CHECK`](#claude_agent_skip_update_check-auto-update-opt-out)). |
| `rebuild_agent [--full]` | Rebuild the resolved image with `--no-cache`, passing `HOST_UID`/`HOST_GID`. With `--full`, rebuild the base `claude-agent` image first, then the project image. Same upstream-update check as `agent` runs first. |
| `create_custom_agent` | Scaffold a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory. |
| `agent_usage [--days N] [--region LABEL] [--refresh]` | Aggregate token usage and estimated USD cost across every project where you've launched `agent` (tracked in `<wrap-dir>/.agent-launches/projects.txt`). Runs on the host. Pricing is fetched from the provider's pricing pages and cached for 7 days. |
| `agent-wrap_update` | Pull the latest wrapper source; if `default-CLAUDE.md` changed, replace the user's copy when unmodified or warn when customized. |

### Container Environment Variables

These vars are set by the wrapper on every `docker run`, regardless of provider (not baked into the image, so overriding them doesn't require a rebuild):

#### Always-injected vars

| Var | Value |
| --- | --- |
| `DISABLE_AUTOUPDATER` | `1` |
| `AGENT_INSTANCE_ID` | `<agent-name>-<uuid>` (also container name + Docker label) |
| `AGENT_NAME` | from `# agent-name:` or sanitized project dir |
| `HOME` | `/home/<agent-user>` (default `/home/ubuntu`) |
| `TERM`, `COLORTERM` | forwarded from host shell |

#### Provider-injected vars

The active provider injects additional vars via `get_agent_env()` and `get_run_args()`. See the provider's README:

- [litellm-bedrock](ops/agent_wrap/providers/litellm_bedrock/README.md)
- [litellm-dashscope](ops/agent_wrap/providers/litellm_dashscope/README.md)

#### Conditional vars

- **Telegram**: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are forwarded when both keys are present in `~/claude_keys.json`. Missing either skips the forwarding.
- **WSLg**: `DISPLAY`, `WAYLAND_DISPLAY`, and `XDG_RUNTIME_DIR` are forwarded on WSL2+WSLg hosts. See [Clipboard & WSLg Support](#clipboard--wslg-support).

## Project Layout

```
.
├── CLAUDE.md                    # Repo-level guidance (for editing this project)
├── README.md
├── Makefile                     # QA targets (test, lint, format, typecheck)
├── pyproject.toml               # Python project config
├── Dockerfile.agent             # Template for project-specific agent images
├── .gitignore
├── agent-wrap.bashrc            # Shell functions: agent, rebuild_agent, create_custom_agent, agent_usage, agent-wrap_update
├── main.py                      # CLI entry point (importlib dispatcher → agent_wrap.commands.*)
├── agent_wrap/                  # Python package — all orchestration logic
│   ├── commands/                # Subcommand implementations (agent, rebuild, create, usage, update)
│   ├── providers/               # Provider plugins
│   │   ├── base.py              # Provider ABC (4 abstract methods)
│   │   ├── litellm_common/      # Shared LiteLLM sidecar base class (internal)
│   │   ├── litellm_bedrock/     # AWS Bedrock (default)
│   │   └── litellm_dashscope/   # Alibaba DashScope
│   ├── config.py                # Settings JSON manipulation (statusline, telegram hooks, dir creation)
│   ├── utils.py                 # Name sanitization, image resolution, Dockerfile.agent parsing
│   └── docker_utils.py          # Docker info queries (rootless detection, image existence)
├── ops/                         # Wrapper ops files (bind-mounted into the container)
│   ├── Dockerfile               # Base image: Ubuntu 24.04 + Node 24 + Claude Code CLI + hadolint + crane + clipboard tooling
│   ├── validate-dockerfile-agent    # Pre-build validator (hadolint, contract checks, crane user probe)
│   ├── statusline.py                # Status bar script (model/cost, context %/update notice)
│   ├── telegram-notify.sh           # PermissionRequest / Stop / StopFailure Telegram notifications
│   ├── md_to_html.js                # Markdown → Telegram-HTML converter used by telegram-notify.sh
│   ├── wl-paste-shim                # WSLg clipboard shim: surfaces Windows-clipboard BMP images as PNG via ImageMagick
│   └── default-CLAUDE.md            # Default instructions (copied into consumer projects' global config)
├── .claude_config/              # Global Claude config (git-ignored, auto-created)
└── .agent-launches/             # Project registry (projects.txt) and pricing cache (git-ignored, auto-created)
                                 # Provider lock/refcount files live under agent_wrap/providers/<name>/
```
