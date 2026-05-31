<!-- This file has been edited with the assistance of an AI tool. -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository provides a Docker-based wrapper for running Claude Code CLI through AWS Bedrock. It packages Claude Code into a container and provides bash functions for easy invocation. Model traffic is routed through a shared LiteLLM sidecar container so observability (Langfuse, etc.) can be layered in without touching Claude Code itself.

## Architecture

- **Dockerfile**: Builds an Ubuntu 24.04-based image with Node.js 24.x and Claude Code CLI installed globally. Also bakes in `hadolint` and `crane` for use by the in-container validator, plus `wl-clipboard`, `xclip`, and `imagemagick` so Claude Code's `Ctrl+V` can paste images from the WSLg-bridged Windows clipboard (ImageMagick is used by the `wl-paste` shim to convert WSLg's BMP-only clipboard images to PNG on the fly). Configured to use AWS Bedrock for Claude API access.
- **agent-wrap.bashrc**: Thin bash dispatcher sourced in your shell. Each function delegates to `python3 main.py <subcommand>`:
  - `agent([--base] [args...])`: Runs Claude Code in Docker with proper volume mounts and credentials. With `--base`, ignores any `Dockerfile.agent` in the current directory and launches the base `claude-agent` image instead (no project-specific `EXPOSE`, `agent-user`, or `agent-run-args` are applied). The flag is consumed by `agent()` itself; remaining args are forwarded to the in-container `claude` CLI.
  - `rebuild_agent([--full])`: Rebuilds the resolved image with `--no-cache`. With `--full`, rebuilds the base `claude-agent` image first, then the project image. Without `--full` in a project whose `Dockerfile.agent` uses `FROM claude-agent` and the base is missing, fails fast with a hint pointing at `--full`. Without `--full` in a project whose `Dockerfile.agent` inherits from a non-`claude-agent` base, prints a one-line migration suggestion but builds normally.
  - `create_custom_agent()`: Scaffolds a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory.
  - `agent_usage()`: Aggregates token usage and estimated cost across every directory the user has launched `agent` in. Reads the project registry at `<wrap-dir>/.agent-launches/projects.txt` (a flat list of absolute paths, appended to by `agent()` on each invocation) and walks each project's `.claude/sessions/*.jsonl` files. Prints an aligned table sorted by cost descending plus per-model and per-day breakdowns (the per-day section defaults to the last 30 calendar days in host-local time; pass `--days N` through to widen, or `--days 0` to show every active day). Runs on the host (only the host can see every project's session data — each container only mounts one project at a time). Implemented as a thin bash wrapper around `agent_usage.py`.
- **agent_wrap/commands/usage.py**: Python script (stdlib only) invoked by the `agent_usage` shell function. Streams session JSONL files, sums `message.usage` token counts grouped by `message.model` and by host-local calendar date, and converts to USD using AWS Bedrock's published pricing — fetched on first run from `aws.amazon.com/bedrock/pricing/` (which is joined with `b0.p.awsstatic.com/.../bedrockfoundationmodels.json` to resolve the page's opaque `priceOf!...` placeholders into real numbers) and cached at `<wrap-dir>/.agent-launches/pricing.json` for 7 days. Region defaults to "US East (N. Virginia)" to match the wrapper's `AWS_REGION=us-east-1`; `--region` and `--refresh` flags can override, and `--days N` controls the per-day window (default 30, `0` = all). Unknown models render their cost as `?` rather than zero. Runs on the host, not mounted into the container.
- **validate-dockerfile-agent**: Shell script mounted read-only into every container at `/opt/agent-wrap/validate-dockerfile-agent`. Validates a project's `Dockerfile.agent` before the agent prompts the user to rebuild: runs hadolint, checks wrapper-contract directives, and confirms the expected in-container user exists — catching "build succeeds, launch fails" scenarios at write time. When the `Dockerfile.agent` uses `FROM claude-agent`, the validator hardcodes that the base provides user `ubuntu`; for other bases it uses `crane` to probe the image's `/etc/passwd` from the registry (no Docker daemon).
- **statusline.py**: Python script mounted read-only at `/opt/agent-wrap/statusline.py` and wired into the user's `settings.json` as the `statusLine` command on first launch. Renders a two-row status line (model/effort/cost on row 1, context-usage %/update-available notice on row 2). If the user removes the `statusLine` key from their `settings.json`, the wrapper re-injects it on the next launch — to customize, redefine the key instead of deleting it.
- **telegram-notify.sh**: Bash script mounted read-only at `/opt/agent-wrap/telegram-notify.sh` and invoked by `PermissionRequest`, `Stop`, and `StopFailure` hooks when Telegram credentials are present in `~/claude_keys.json`. Sends a Telegram message when Claude asks for permission, finishes responding, or hits an API error. Hook entries are idempotently injected into `settings.json` by `_agent_ensure_telegram_hooks()` on each `agent()` launch when creds are configured.
- **md_to_html.js**: Node script mounted read-only at `/opt/agent-wrap/md_to_html.js` and invoked by `telegram-notify.sh` to convert Markdown into Telegram's subset of HTML (bold/italic/code/links/strikethrough + `<pre>` with optional language tag for code blocks).
- **wl-paste-shim**: Bash shim mounted read-only at `/usr/local/bin/wl-paste` (only when `/mnt/wslg` exists on the host) so it shadows the real `/usr/bin/wl-paste` via PATH order. WSLg advertises Windows clipboard images as `image/bmp` only, but Claude Code's `Ctrl+V` paste handler asks for `image/png` and doesn't fall back. The shim intercepts `--list-types` (advertises `image/png` when only BMP is on clipboard) and `--type image/png` (fetches BMP and pipes through `convert bmp:- png:-`), and falls through to the real binary for everything else.
- **agent_wrap/**: Python package containing all orchestration logic. `main.py` at the repo root is the CLI entry point (argparse dispatcher). Subpackages:
  - **commands/**: One module per subcommand (`agent.py`, `rebuild.py`, `create.py`, `usage.py`, `update.py`).
  - **providers/**: Provider plugin tree. `base.py` defines the `Provider` ABC (4 abstract methods: `ensure`, `release`, `get_run_args`, `get_label_args`). Each provider is a subdirectory with `provider.py` + `config.yaml`. `litellm_common/provider.py` implements the shared LiteLLM sidecar lifecycle (~350 lines); `litellm_bedrock/` and `litellm_dashscope/` are thin overrides (~60 lines each). Auto-discovery in `__init__.py` scans `*/provider.py` for concrete `Provider` subclasses (`inspect.isabstract()` filters out the base classes). Selected by `AGENT_PROVIDER` env var (default `litellm-bedrock`).
  - **config.py**: Settings JSON manipulation (statusline injection, telegram hooks, project directory creation).
  - **utils.py**: Name sanitization, image resolution, UUID generation, Dockerfile.agent parsing.
  - **docker_utils.py**: Docker info queries (rootless detection, image existence checks).
- **main.py**: CLI entry point at repo root. Dispatches `agent`, `rebuild`, `create`, `usage`, `update` subcommands to `agent_wrap.commands.*`.

## Key Configuration

### Environment Variables (set on the agent container at `docker run` time)

The four proxy-binding env vars below are produced by the active provider (default: `agent_wrap/providers/litellm_bedrock/provider.py`) and injected via `get_run_args()` (spliced into `docker run` by the agent command). The launcher itself doesn't know about them — that keeps the orchestration agnostic to which provider implementation a fork uses.

- `CLAUDE_CODE_USE_BEDROCK=1`: Enables AWS Bedrock integration in Claude Code
- `AWS_REGION=us-east-1`: Default AWS region (kept for parity; the sidecar is the one that actually talks to Bedrock). Overriding this on the host does **not** repoint the sidecar's upstream Bedrock region — both values are pinned together inside `agent_wrap/providers/litellm_bedrock/provider.py` (the agent's `AWS_REGION` in `get_agent_env`, the sidecar's `AWS_REGION_NAME` in `get_sidecar_env`). To target a different region, fork both spots together.
- `AWS_BEARER_TOKEN_BEDROCK`: the **LiteLLM sidecar's master key**, not the user's AWS bearer token. The user's actual Bedrock key goes only to the sidecar.
- `ANTHROPIC_BEDROCK_BASE_URL`: `http://agent-wrap-litellm:4000/bedrock` (the sidecar's container name on the shared user-defined Docker network `agent-wrap-net`). When the agent runs in the host network namespace (`AGENT_USE_HOST_NETWORK=1` or `--network host` in `agent-run-args`), the same hostname is resolved via an injected `--add-host` entry — pointing at `127.0.0.1` if the sidecar is also in host mode, otherwise at the sidecar's bridge IP or the host gateway depending on the running mode. Points Claude Code at the sidecar's Bedrock passthrough endpoint.

The launcher itself sets these directly:

- `AGENT_INSTANCE_ID`: per-launch identifier of the form `<agent-name>-<uuid>` (where `<agent-name>` is derived from the `# agent-name:` directive or a sanitized `basename $(pwd)`). Also applied as the `agent-wrap.instance-id` Docker label and as the container name (`claude-agent-<AGENT_INSTANCE_ID>`).
- `DISABLE_AUTOUPDATER=1`: Disables the Claude Code in-container auto-updater

These are injected via `-e` on each launch rather than baked into the image so users can override them (e.g., point at a different region) without rebuilding.

### Sidecar networking

The sidecar lives on a Docker user-defined bridge named `agent-wrap-net` (created on demand by `LiteLLMProvider.ensure()`). It is not published on a host port — agents reach it directly over Docker networks:

- **Default-network agent** (no `--network` in `agent-run-args`): `ensure()` populates `get_run_args()` with the proxy-binding `-e` env vars plus `--network agent-wrap-net`, so the agent joins the same network and resolves `agent-wrap-litellm` by container DNS.
- **Custom-network agent** (`Dockerfile.agent` declares `--network myproj` via `agent-run-args`): the launcher parses the network name out of the args and passes it to `ensure()`, which `docker network connect`s the sidecar to that network so the same container-name URL resolves on the project's network.
- **`AGENT_USE_HOST_NETWORK=1`**: the agent runs in the host network namespace, and `ensure()` also launches the **sidecar** with `--network host` so the proxy's own outbound Bedrock traffic escapes the bridge / FORWARD chain (otherwise the flag would only fix half the path). The agent reaches the sidecar via `--add-host agent-wrap-litellm:127.0.0.1`. Mode is decided at cold-start time and is **first-launch-wins**: a later launch without the flag inherits the running mode rather than fighting it. The sidecar binds the WSL distro's port 4000 in this mode — health-poll catches the failure cleanly if anything else is already listening there.
- **Cross-mode reuse** (bridge-mode agent finds a host-mode sidecar already running, or vice versa): the launcher adapts. A bridge-mode agent reaching a host-mode sidecar uses `--add-host agent-wrap-litellm:host-gateway` (Docker 20.10+'s magic resolver for the host's IP from inside a bridge container).

This sidesteps the FORWARD=DROP scenario triggered by parallel WSL2 distros' dockerds fighting over iptables-legacy rules — agent traffic to the sidecar (and the sidecar's traffic to Bedrock, in host mode) stays inside the namespace it's already on rather than flowing through the host's FORWARD chain.

When `/mnt/wslg` exists on the host (WSL2 + WSLg), `agent()` additionally forwards `DISPLAY` and `WAYLAND_DISPLAY` from the host shell and sets `XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir` so Wayland/X11 clipboard clients in the container reach WSLg's sockets. On non-WSL hosts the block is a no-op.

### `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent()` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

Use this when running multiple WSL2 distros that each run their own `dockerd`. The two daemons share one kernel and fight over `iptables-legacy` rules — the second daemon's startup flips the legacy `FORWARD` policy to `DROP`, stranding the first distro's containers (parent shell stays online; only forwarded/routed traffic dies). `--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces (e.g. `eth6`), not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. In-container services should bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).
- The flag also extends to the LiteLLM sidecar — when set on the **cold-start** launch, the sidecar is launched with `--network host` and binds the WSL distro's port 4000. First-launch-wins: subsequent launches without the flag adapt to the running sidecar's mode rather than restarting it. To switch a running sidecar's mode, stop it (`docker stop agent-wrap-litellm`) and start the next launch with the desired flag value.

### `CLAUDE_AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent()` and `rebuild_agent()` perform a best-effort upstream check on every invocation: `git fetch` against the wrap-dir's tracking branch, and if `HEAD` is behind, prompt the user `Update agent-wrap now? [y/N]`. On accept, the wrapper runs `agent-wrap_update` and returns without launching/rebuilding — re-source `agent-wrap.bashrc` and re-run the original command afterwards (per the spec, an accepted update intentionally does not chain into the original action). Decline with `n`/Enter and the original command runs as usual.

Set `CLAUDE_AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. Any unexpected condition — non-git wrap-dir, detached HEAD, `git fetch` failure or 10s timeout — also silently skips the check, so a flaky network never blocks a launch.

Other wrap functions (`agent_usage`, `create_custom_agent`, `agent-wrap_update` itself) do not perform the check.

### Volume Mounts (in agent function)

The wrapper mirrors a minimal `$HOME` layout into the container at `/home/<agent-user>` (default `ubuntu`). `HOME` is set to that path so Claude Code finds its config in the usual spot.

- `<wrap-dir>/.claude_config/.claude.json` → `/home/<user>/.claude.json` (global Claude config file)
- `<wrap-dir>/.claude_config/.claude/` → `/home/<user>/.claude/` (global Claude directory: `CLAUDE.md`, `settings.json`, etc.)
- `$(pwd)` → `/workspace` (project files)
- `$(pwd)/.claude/sessions/` → `/home/<user>/.claude/projects/-workspace/` (per-project session transcripts, overlays on top of the global `.claude` mount)
- `$(pwd)/.claude/session-state/` → `/home/<user>/.claude/sessions/` (per-project live-session registry — pid/sessionId/cwd/status records; distinct from transcripts)
- `$(pwd)/.claude/daemon/` → `/home/<user>/.claude/daemon/` (per-project supervisor/worker roster — sockets, dispatch, env)
- `$(pwd)/.claude/jobs/` → `/home/<user>/.claude/jobs/` (per-project bg-job state)
- `$(pwd)/.claude/daemon.lock` → `/home/<user>/.claude/daemon.lock` (per-project supervisor lock file)
- `$(pwd)/.claude/daemon.log` → `/home/<user>/.claude/daemon.log` (per-project supervisor log)
- `$(pwd)/.claude/daemon.status.json` → `/home/<user>/.claude/daemon.status.json` (per-project supervisor status snapshot)
- `$(pwd)/.claude/history.jsonl` → `/home/<user>/.claude/history.jsonl` (per-project shell-prompt history)
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

`ServiceCredentialSecret` is the user's AWS Bedrock bearer token. It is passed only to the LiteLLM sidecar (as `AWS_BEARER_TOKEN_BEDROCK` on the sidecar container); claude-agent never sees it. Inside claude-agent, `AWS_BEARER_TOKEN_BEDROCK` is the proxy's auto-generated master key — the boundary between Claude Code and the proxy is bearer-on-Bearer, not SigV4.

`TelegramBotToken` and `TelegramChatId` are optional. If both are present, the wrapper forwards them as env vars into the container and injects `PermissionRequest` / `Stop` / `StopFailure` hooks into `settings.json` so Telegram notifications fire. If either is missing, no hooks are injected and no env vars are set (the script would no-op anyway).

### LiteLLM sidecar lifecycle

A single shared `agent-wrap-litellm` Docker container fronts AWS Bedrock for every claude-agent launch on this host. It is **not** built by `rebuild_agent`; the wrapper pulls a pinned upstream image directly. Lifecycle:

- **Lazy start**: the first `agent` launch creates the user-defined `agent-wrap-net` bridge (idempotent) and starts the sidecar attached to it (under `flock` on `agent_wrap/providers/<provider>/lock`) with a Docker `--health-cmd` that hits `/health/liveliness` from inside the container, and waits up to ~90 s for `.State.Health.Status` to flip to `healthy`. The sidecar publishes no host port — agents reach it over the shared bridge.
- **Network attach (per-launch)**: if the agent will run on a project-supplied network (`--network X` in `agent-run-args`), `ensure()` `docker network connect`s the sidecar to that network on the agent's launch so the agent reaches `agent-wrap-litellm` by container DNS without leaving its own bridge.
- **Refcount**: each running claude-agent registers its `AGENT_INSTANCE_ID` in `agent_wrap/providers/<provider>/refcount`. Parallel agents share the one sidecar.
- **Refcount-based stop**: when the last agent exits and the refcount file is empty, the sidecar is stopped. Stale entries (from killed launches) are reconciled against `docker ps --filter label=agent-wrap.role=claude-agent` on every release.
- **Master key**: minted in memory on first start and passed to the sidecar via `-e LITELLM_MASTER_KEY=…`. Subsequent launches that find the sidecar already running recover it via `docker inspect` rather than reading from disk. Consequence: a manual `docker stop`/`restart` of the sidecar mints a fresh key on its next start, which would 401 any in-flight agents holding the old one — but `--rm` plus the refcount-driven stop already imply teardown of those agents, so this matches the actual fault model.
- **Failure mode**: any failure during `ensure()` aborts the agent launch loudly and dumps the sidecar's recent logs. There is no fallback to direct Bedrock — that would mask a misconfigured proxy.

To bump the LiteLLM version, change the `image` class attribute in `agent_wrap/providers/litellm_common/provider.py` or in the specific provider override (tag + digest) and, if any of the lifecycle behavior changed in upstream, update the base class accordingly.

### Provider plugin selection

`main.py` resolves exactly one provider per invocation, selected by the `AGENT_PROVIDER` env var. The default is `litellm-bedrock`, preserving historical behavior. Forks that want a different routing implementation (direct Anthropic, Vertex, a hosted LiteLLM, etc.) should create `agent_wrap/providers/<name>/` with `provider.py` and `config.yaml`, then set `AGENT_PROVIDER=<name>`. The auto-discovery in `agent_wrap/providers/__init__.py` scans all `*/provider.py` for concrete `Provider` subclasses and fails fast if the requested provider isn't found.

The provider contract is the `Provider` ABC in `agent_wrap/providers/base.py` — 4 abstract methods (`ensure`, `release`, `get_run_args`, `get_label_args`). The shared LiteLLM sidecar lifecycle lives in `agent_wrap/providers/litellm_common/provider.py`; new LiteLLM-based providers subclass `LiteLLMProvider` and only override auth/env specifics.

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
