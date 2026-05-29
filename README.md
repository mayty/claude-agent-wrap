<!-- This file has been edited with the assistance of an AI tool. -->
# claude-agent-wrap

A Docker-based wrapper for running the [Claude Code](https://github.com/anthropics/claude-code) CLI against AWS Bedrock. It packages Claude Code into a reproducible container image and exposes two bash functions — `agent` and `rebuild_agent` — that handle volume mounts, credentials, and per-project image customization. Model traffic is routed through a shared [LiteLLM](https://github.com/BerriAI/litellm) sidecar container that fronts AWS Bedrock — see [LiteLLM sidecar](#litellm-sidecar).

## Why

Running Claude Code in a container isolates the tool from your host system, pins its dependencies, and lets each project override the base image with its own `Dockerfile.agent` to supply project-specific runtime requirements. The wrapper also routes the CLI through AWS Bedrock instead of the Anthropic API, so auth is an AWS bearer token rather than an Anthropic API key. A single shared LiteLLM proxy container sits between Claude Code and Bedrock so the user's Bedrock token never enters the agent container.

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

If a `Dockerfile.agent` exists in the current directory but you want to run against the base `claude-agent` image for this launch (e.g., to bypass a broken project image or compare behavior), pass `--base`:

```bash
agent --base [claude-code-args...]
```

The wrapper mounts:

| Host | Container | Purpose |
| --- | --- | --- |
| `$(pwd)` | `/workspace` | Project files |
| `<wrap-dir>/.claude_config/.claude.json` | `/home/<user>/.claude.json` | Global Claude config file |
| `<wrap-dir>/.claude_config/.claude` | `/home/<user>/.claude` | Global Claude directory (`CLAUDE.md`, `settings.json`, caches, etc.) |
| `$(pwd)/.claude/sessions` | `/home/<user>/.claude/projects/-workspace` | Per-project session history (overlays the global `.claude` mount) |
| `$(pwd)/.claude/{plans,todos,tasks,shell-snapshots,session-env,file-history,paste-cache}` | `/home/<user>/.claude/<same>` | Per-project state overlays (plans, todos, tasks, shell snapshots, session env, file history, paste cache) |
| `/mnt/wslg`, `/mnt/wslg/.X11-unix`, `<wrap-dir>/wl-paste-shim` | `/mnt/wslg`, `/tmp/.X11-unix`, `/usr/local/bin/wl-paste` | WSL2 + WSLg only — Wayland/X11 sockets and the `wl-paste` shim that surfaces Windows-clipboard images as PNG. See [Clipboard / WSLg](#clipboard--wslg). |

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

## LiteLLM sidecar

A single shared `agent-wrap-litellm` container fronts AWS Bedrock for every `agent` launch on this host. Claude Code talks to the sidecar; the sidecar talks to Bedrock. This is the auth and traffic boundary every agent on the host shares.

### What it is

- A pinned upstream LiteLLM image (tag + digest in `providers/litellm-bedrock/provider.sh`) — **not** built by `rebuild_agent`; the wrapper pulls it directly.
- Started lazily on the first `agent` launch (under `flock`), waited on via Docker's built-in healthcheck, and stopped automatically when the last `agent` exits. State (lock file, refcount of running agents) lives under `<wrap-dir>/.agent-launches/`.
- Parallel `agent` launches share the one sidecar — each registers its `AGENT_INSTANCE_ID` in the refcount file.

### Auth boundary

The user's AWS Bedrock bearer token (`ServiceCredentialSecret` in `~/claude_keys.json`) is read by the sidecar script and passed only to the sidecar container. The agent container never sees it. Inside the agent, `AWS_BEARER_TOKEN_BEDROCK` is the **proxy's auto-generated master key**, not the AWS token — Claude Code presents it as a Bearer token to the sidecar in place of an AWS SigV4 header.

The master key is minted in memory on first start and recovered via `docker inspect` on subsequent launches that find the sidecar already running. It is never written to disk.

### Networking

The sidecar lives on a Docker user-defined bridge named `agent-wrap-net` (created on demand). It is not published on a host port — agents reach it directly over Docker networks:

- **Default agent**: joins `agent-wrap-net` and resolves the sidecar by container name (`agent-wrap-litellm`).
- **Custom-network agent** (`Dockerfile.agent` declares `--network myproj` via `# agent-run-args:`): the sidecar is `docker network connect`'d to that network at launch so the same container-name URL still resolves.
- **`AGENT_USE_HOST_NETWORK=1`**: the agent **and** the sidecar both run with `--network host`, so the proxy's outbound Bedrock traffic also escapes the bridge / FORWARD chain. Mode is decided at the sidecar's cold start and is **first-launch-wins**: a later launch without the flag inherits the running mode rather than fighting it. To switch a running sidecar's mode, stop it (`docker stop agent-wrap-litellm`) and re-launch with the desired flag value.

### Customizing

- `providers/litellm-bedrock/provider.sh` — image pin, lifecycle (start, healthcheck-wait, refcount, shutdown). Forks that swap the proxy implementation should drop in a new `providers/<name>/provider.sh` and select it via `AGENT_PROVIDER=<name>`; its public contract with `agent-wrap.bashrc` is intentionally narrow so upstream syncs stay clean.
- `providers/litellm-bedrock/config.yaml` — LiteLLM proxy config. Phase-1 setup is a Bedrock passthrough wildcard (`bedrock/*`) plus the master-key binding.
- `providers/template/` — copy this directory to `providers/<your-name>/` when adding a new provider. Carries failing stubs of the three contract functions plus a README documenting the lifecycle.

## Clipboard / WSLg

On WSL2 hosts with WSLg enabled (detected via `[ -d /mnt/wslg ]`), `agent` automatically wires the container into the host's clipboard and display sockets so Claude Code's `Ctrl+V` paste of Windows-clipboard images works out of the box. Specifically, the wrapper:

- bind-mounts `/mnt/wslg` and `/mnt/wslg/.X11-unix` (the latter at `/tmp/.X11-unix`, the conventional X11 socket path),
- forwards `DISPLAY` and `WAYLAND_DISPLAY` from the host shell and sets `XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir`, and
- bind-mounts `wl-paste-shim` over `/usr/local/bin/wl-paste` so it shadows the real binary via PATH order.

The shim is needed because WSLg advertises Windows-clipboard images as `image/bmp` only, while Claude Code's paste handler asks for `image/png`. The shim intercepts `--list-types` (advertises `image/png` when only BMP is on the clipboard) and `--type image/png` (fetches BMP and pipes through ImageMagick's `convert bmp:- png:-`), and falls through to the real `wl-paste` for everything else.

On macOS or native Linux hosts the entire block is a no-op.

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
| `agent [--base] [args...]` | Run Claude Code in a container against the resolved image for the current directory. With `--base`, ignore any `Dockerfile.agent` in the current directory and launch the base `claude-agent` image instead (project-specific `EXPOSE`, `agent-user`, and `agent-run-args` directives are skipped). On every invocation, performs a best-effort upstream check and prompts to pull if the wrap-dir is behind (see [`CLAUDE_AGENT_SKIP_UPDATE_CHECK`](#claude_agent_skip_update_check-auto-update-opt-out)). |
| `rebuild_agent [--full]` | Rebuild the resolved image with `--no-cache`, passing `HOST_UID`/`HOST_GID`. With `--full`, rebuild the base `claude-agent` image first, then the project image. Same upstream-update check as `agent` runs first. |
| `create_custom_agent` | Scaffold a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory. |
| `agent_usage [--days N] [--region LABEL] [--refresh]` | Aggregate token usage and estimated USD cost across every project where you've launched `agent` (tracked in `<wrap-dir>/.agent-launches/projects.txt`). Runs on the host — only the host can see every project's session data, since each container only mounts one project at a time. Pricing is fetched from AWS's Bedrock pricing pages and cached for 7 days. |
| `agent-wrap_update` | Pull the latest wrapper source; if `default-CLAUDE.md` changed, replace the user's copy when unmodified or warn when customized. |

## Environment

The `agent()` function injects these env vars on each `docker run` (not baked into the image, so overriding them doesn't require a rebuild):

- `CLAUDE_CODE_USE_BEDROCK=1` — routes Claude Code through AWS Bedrock.
- `AWS_REGION=us-east-1` — kept for parity with Claude Code's expectations. The agent doesn't reach Bedrock directly; the **sidecar** does, and pins its own upstream region in `providers/litellm-bedrock/provider.sh`. Overriding this on the host does not repoint the sidecar.
- `AWS_BEARER_TOKEN_BEDROCK` — the **LiteLLM sidecar's master key** (auto-generated per cold-start), not the user's AWS bearer token. Claude Code presents it to the sidecar as a Bearer token; the sidecar uses its own credentials to reach Bedrock.
- `ANTHROPIC_BEDROCK_BASE_URL` — `http://agent-wrap-litellm:4000/bedrock`. Points Claude Code at the sidecar's Bedrock passthrough.
- `AGENT_INSTANCE_ID` — per-launch identifier of the form `<agent-name>-<uuid>`. Also applied as the container name (`claude-agent-<AGENT_INSTANCE_ID>`) and as the `agent-wrap.instance-id` Docker label, so `docker ps` and the sidecar refcount can identify each launch.
- `DISABLE_AUTOUPDATER=1` — disables the Claude Code in-container auto-updater.
- `AGENT_NAME` — derived from `# agent-name:` (or the project directory name when no `Dockerfile.agent` exists); available to in-container scripts that want to identify the agent.
- `HOME` — set to `/home/<agent-user>` so Claude Code finds its config in the usual spot.
- `TERM`, `COLORTERM` — forwarded from the host shell so terminal colors render correctly.

The user's AWS Bedrock bearer token is read from `~/claude_keys.json` and passed only to the LiteLLM sidecar; the agent container receives the proxy's auto-generated master key under the same `AWS_BEARER_TOKEN_BEDROCK` env var name. See [LiteLLM sidecar](#litellm-sidecar).

If both `TelegramBotToken` and `TelegramChatId` are present in `~/claude_keys.json`, they are forwarded into the container as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` and consumed by the notification hooks. Missing either one skips the forwarding entirely.

On WSL2 + WSLg hosts (when `/mnt/wslg` exists), `DISPLAY` and `WAYLAND_DISPLAY` are forwarded from the host shell and `XDG_RUNTIME_DIR` is set to `/mnt/wslg/runtime-dir` so Wayland/X11 clipboard clients in the container reach WSLg's sockets. See [Clipboard / WSLg](#clipboard--wslg).

### `AGENT_PROVIDER` (model-routing backend)

`agent-wrap.bashrc` sources exactly one provider plugin per session, selected by `AGENT_PROVIDER`. Each provider lives in `providers/<name>/provider.sh` and implements three functions plus one output array — see [`providers/template/README.md`](providers/template/README.md) for the contract. The default is `litellm-bedrock`, preserving historical behavior.

```sh
# Use the default LiteLLM-Bedrock provider (no var needed)
agent

# Or pick a different one — the launcher fails fast and lists available
# providers if the directory doesn't exist.
AGENT_PROVIDER=my-direct-anthropic source agent-wrap.bashrc
agent
```

Providers are auto-discovered by globbing `providers/*/provider.sh` — drop in a new directory and it shows up in the error message above without any registry edits.

### `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

Use this when you run multiple WSL2 distros that each have their own `dockerd`. All WSL2 distros share a single Linux kernel, so the two daemons fight over the kernel's iptables tables — specifically, the second daemon to start installs Docker's standard ruleset on `iptables-legacy`, which flips the legacy `FORWARD` chain policy from `ACCEPT` to `DROP`. Reply traffic to the first distro's existing containers then gets dropped before it reaches `docker0`. Symptom: parent shell stays online, but containers lose all outbound TCP (DNS UDP still works); recovery requires `wsl --shutdown`. Relaunching the container does not help, because the broken state is upstream of `docker0`.

`--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and the FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces, not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. Make in-container services bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).
- The flag also extends to the LiteLLM sidecar — when set on the **cold-start** launch, the sidecar is launched with `--network host` and binds the WSL distro's port 4000. First-launch-wins: subsequent launches without the flag adapt to the running sidecar's mode rather than restarting it. To switch a running sidecar's mode, stop it (`docker stop agent-wrap-litellm`) and start the next launch with the desired flag value.

### `CLAUDE_AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent` and `rebuild_agent` run a best-effort upstream check on every invocation: a `git fetch` against the wrap-dir's tracking branch, then — if `HEAD` is behind — a `Update agent-wrap now? [y/N]` prompt. On `y`, the wrapper runs `agent-wrap_update`, re-sources `agent-wrap.bashrc` in the parent shell so the new function definitions take effect immediately, and returns without launching the container or rebuilding the image; re-run your original command afterwards. On `n` (or Enter), the original command proceeds unchanged.

Set `CLAUDE_AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. The check is also auto-skipped on any error path — non-git wrap-dir, detached HEAD, fetch failure, or 10-second fetch timeout — so a flaky or offline network never blocks a launch.

Other wrap functions (`agent_usage`, `create_custom_agent`, and `agent-wrap_update` itself) do not perform the check.

## Layout

```
.
├── Dockerfile                   # Base image: Ubuntu 24.04 + Node 24 + Claude Code CLI + hadolint + crane + clipboard tooling
├── agent-wrap.bashrc            # Shell functions: agent, rebuild_agent, create_custom_agent, agent_usage, agent-wrap_update
├── validate-dockerfile-agent    # Pre-build validator (hadolint, contract checks, crane user probe)
├── statusline.py                # Status bar script (model/cost, context %/update notice)
├── telegram-notify.sh           # PermissionRequest / Stop / StopFailure Telegram notifications
├── md_to_html.js                # Markdown → Telegram-HTML converter used by telegram-notify.sh
├── agent_usage.py               # Host-side usage/cost aggregator invoked by agent_usage (Bedrock pricing fetched + cached)
├── wl-paste-shim                # WSLg clipboard shim: surfaces Windows-clipboard BMP images as PNG via ImageMagick
├── providers/                   # Provider plugin tree — one subdirectory per model-routing backend; selected by AGENT_PROVIDER env var (default: litellm-bedrock)
│   ├── template/
│   │   ├── provider.sh          # Failing stubs of the three contract functions; copy this dir to add a new provider
│   │   └── README.md            # Provider contract docs: function args, lifecycle, output array, file layout
│   └── litellm-bedrock/
│       ├── provider.sh          # Default provider: LiteLLM sidecar lifecycle (lazy start, healthcheck wait, refcount, shutdown). Image pinned by tag+digest.
│       └── config.yaml          # LiteLLM proxy config (Bedrock /bedrock/* passthrough, master-key auth)
├── default-CLAUDE.md            # Default instructions (copied into consumer projects' global config)
├── CLAUDE.md                    # Repo-level guidance (for editing this project)
├── README.md
├── .claude_config/              # Global Claude config (git-ignored, auto-created)
└── .agent-launches/             # Project registry (projects.txt), Bedrock pricing cache, and LiteLLM sidecar state (litellm.lock, litellm.refcount) (git-ignored, auto-created)
```
