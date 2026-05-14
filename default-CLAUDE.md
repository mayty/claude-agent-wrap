<!-- This file has been edited with the assistance of an AI tool. -->
# Global instructions

## Environment

You are running inside a Docker container managed by the `agent-wrap` tooling. The container is built from a `Dockerfile.agent` in the project root (or from the base `Dockerfile` if none exists). Filesystem changes inside the container are discarded when it exits — only `/workspace` and the Claude home directory persist.

Within the Claude home directory (`$HOME/.claude/`), most paths (`settings.json`, `CLAUDE.md`, `themes/`, `cache/`, `history.jsonl`, etc.) are backed by a *shared* global mount on the host, so they persist across every project. A specific set of subdirectories is instead overlaid with a *per-project* mount rooted at `$(pwd)/.claude/<subdir>/` on the host — currently: `projects/-workspace/` (session history, mounted from `$(pwd)/.claude/sessions/`), `plans/`, `todos/`, `tasks/`, `shell-snapshots/`, `session-env/`, `file-history/`, and `paste-cache/`. Content you write under those paths is visible only within this project, not in other projects' agent sessions. Don't rely on finding another project's plans or todos by reading `~/.claude/plans/` — they won't be there.

The wrapper's own source is mounted read-only at `/opt/agent-wrap/` — `Dockerfile` (the base image), `agent-wrap.bashrc` (the launcher, including the `agent`, `rebuild_agent`, and `create_custom_agent` functions), `validate-dockerfile-agent` (a validator script, see below), `statusline.py` (the default Claude Code status-line script, auto-wired into `settings.json` on first launch — to customize, redefine the `statusLine` key in `settings.json`; deleting the key will cause it to be re-injected on the next launch), and `telegram-notify.sh` + `md_to_html.js` (invoked by `PermissionRequest`/`Stop`/`StopFailure` hooks when the user has configured Telegram credentials in `~/claude_keys.json` — the hook entries are auto-injected into `settings.json` when creds are present; don't treat the hook entries or mount references as stale). Consult these files as the source of truth if the guidance below is ambiguous or you suspect it has drifted from actual behavior; otherwise prefer the summary here.

**Important:** You always run as a non-root user inside the container and are never granted `sudo` access. Do not attempt to use `sudo` or assume root privileges. If a task requires elevated permissions, instruct the user to add the necessary `RUN` steps to their `Dockerfile.agent` instead.

**Clipboard:** the base image ships `wl-clipboard` and `xclip`, and on WSL2 + WSLg hosts the wrapper auto-mounts `/mnt/wslg` and forwards `DISPLAY`/`WAYLAND_DISPLAY`/`XDG_RUNTIME_DIR`. Claude Code's `Ctrl+V` for Windows-clipboard images works out of the box — do not add clipboard packages or WSLg mounts to a `Dockerfile.agent`.

## Installing dependencies

Do **not** install dependencies ad-hoc inside the running container (`apt-get install`, `pip install`, `npm install -g`, etc.). Those changes are thrown away as soon as the session ends.

Instead:

- **If `Dockerfile.agent` exists in the project root:** edit it to add the dependency (e.g., add a `RUN apt-get install -y <pkg>` line), then tell the user to run `rebuild_agent` and restart the session.
- **If there is no `Dockerfile.agent`:** create it by copying the canonical base image from `/opt/agent-wrap/Dockerfile` to `/workspace/Dockerfile.agent`, prepend a `# agent-name: <name>` line, then add your `RUN` steps. Derive `<name>` from the project directory (`basename /workspace`), lowercased, with any character outside `[a-z0-9_.-]` replaced by `-` and leading/trailing dashes stripped (Docker image names must be lowercase). Do **not** write `Dockerfile.agent` from scratch — the base image sets up Node, the Claude CLI, and `WORKDIR /workspace` that the wrapper depends on. Once edited, prompt the user to run `rebuild_agent` and restart the session.

Project-level (language) dependencies that belong in the project's own manifest (`package.json`, `requirements.txt`, `go.mod`, `Cargo.toml`, etc.) can be installed normally — those live in `/workspace` and persist.

**Note:** If a tool you need is unavailable in the environment, add it to `Dockerfile.agent` (and prompt the user to rebuild) rather than working around its absence — e.g., don't reach for `curl` when `wget` is missing, don't hand-roll JSON parsing because `jq` isn't installed, don't script with `sed`/`awk` when the right tool is a one-line install. The only exception is when the user explicitly asks for a workaround.

## Per-project customization via `Dockerfile.agent`

`Dockerfile.agent` is not just a place to install packages — it also carries directives that `agent-wrap` reads to customize the runtime. Use them instead of asking the user to change their shell invocation or the global tooling.

**Important:** The working directory must always be `WORKDIR /workspace`. Do not change it — the wrapper mounts the project to `/workspace` and the agent expects to operate from there.

Recognized directives (as special comments in `Dockerfile.agent`):

- **`# agent-name: <name>`** — required. Tags the built image as `claude-agent-<name>`. Must match `[a-z0-9_.-]+` (Docker image names are lowercase). Don't change this casually — renaming it orphans the previously built image.
- **`# agent-user: <username>`** — sets the container username (default: `ubuntu`). This reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`, and sets `HOME` accordingly. Use this when your custom image creates a different user than the base `ubuntu` user. Make sure the user exists in your `Dockerfile.agent` (e.g., via `useradd` with `ARG HOST_UID`/`ARG HOST_GID`) and that `/home/<username>` is writable by that user.
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each is whitespace-split into tokens (no shell quoting, so args containing spaces cannot be expressed). Use this when the container needs extra capabilities, devices, or mounts. Example:

  ```dockerfile
  # agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN
  ```

- **`EXPOSE <port>`** — any `EXPOSE` line in `Dockerfile.agent` is automatically published to `127.0.0.1:<port>` on the host. Use this for dev servers, debuggers, etc. — don't ask the user to add `-p` flags manually.
- **`ARG HOST_UID` / `ARG HOST_GID`** — `rebuild_agent` always passes the host user's UID/GID as build args. Declare these in `Dockerfile.agent` if you need them at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory baked into the image).

When a user asks for something that a `docker run` flag would solve (mounting a path, exposing a port, adding a capability, passing a device), add the appropriate directive to `Dockerfile.agent` and prompt them to run `rebuild_agent` — do not tell them to edit `agent-wrap.bashrc` or their shell invocation.

Security note: `agent-run-args` is pass-through to `docker run`, so it can grant `--privileged`, host mounts, and similar. Only add flags you actually need, and flag to the user what you're granting and why.

**Forbidden without explicit user request:** do not add `-v /var/run/docker.sock:/var/run/docker.sock` (or any other mount of the host Docker socket) on your own initiative. Access to the host Docker socket is equivalent to unrestricted root on the host — a process in the container can launch privileged containers, bind-mount `/`, and fully compromise the host, defeating the isolation the wrapper is meant to provide. Only add it if the user explicitly asks for Docker-in-Docker / socket access, and before doing so, spell out to them that it grants host-root-equivalent access and confirm they still want it. The same rule applies to other escape-hatch flags like `--privileged`, `--pid=host`, `--network=host`, or bind-mounts of sensitive host paths (`/`, `/etc`, `/root`, `~/.ssh`, cloud credential dirs).

**Strongly recommended: shadow build/cache directories with anonymous volumes.** Because `/workspace` is bind-mounted from the host, anything the container writes under it — `node_modules/`, `.venv/`, `target/`, `dist/`, `__pycache__/`, `.next/`, `.pytest_cache/`, etc. — lands on the host filesystem. That pollutes the host, slows down host-side tools (editors, file watchers, git status), and can cause cross-platform breakage when host and container OS/arch differ (e.g., Linux-built `node_modules` native bindings on a macOS host). Prefer keeping these inside the container by adding anonymous volume mounts via `agent-run-args`:

```dockerfile
# agent-run-args: -v /workspace/node_modules
# agent-run-args: -v /workspace/.venv
# agent-run-args: -v /workspace/target
```

An anonymous volume (a `-v` flag with only a container path, no host path) shadows that sub-path of the bind mount so writes go into a Docker-managed volume instead of the host `/workspace`. Pick the directories that match the project's build system: `node_modules` for Node, `.venv`/`__pycache__` for Python, `target` for Rust, `build`/`dist` for most bundlers, etc. When you set up or modify a `Dockerfile.agent` for a project, proactively add these for the languages in use.

## Validating `Dockerfile.agent` before rebuild

**Always run `/opt/agent-wrap/validate-dockerfile-agent` after you create or edit `Dockerfile.agent`, and before you tell the user to run `rebuild_agent`.** The validator catches mistakes that `docker build` alone won't — most importantly, base images that don't contain the user the wrapper will try to use. A build can succeed and still produce an image the wrapper cannot launch (mounts land on a nonexistent `/home/<user>`), so catch these issues up front.

The validator runs three layers of checks:

1. **hadolint** — generic Dockerfile hygiene.
2. **Wrapper-contract checks** — `# agent-name:` format, `WORKDIR /workspace` preserved, quoted whitespace in `# agent-run-args:`, `ARG HOST_UID`/`ARG HOST_GID` declared if referenced.
3. **Base-image user probe** — uses `crane` (no Docker daemon needed) to fetch the base image's `/etc/passwd` directly from the registry and confirm the expected in-container user (`# agent-user:` value, or `ubuntu` by default) actually exists there. If it doesn't, the validator lists the non-root users it found in the base so you can pick a valid one.

Usage:

```sh
/opt/agent-wrap/validate-dockerfile-agent           # validates ./Dockerfile.agent
/opt/agent-wrap/validate-dockerfile-agent path/to/Dockerfile.agent
```

Exit codes: `0` pass (warnings allowed), `1` errors, `2` file missing or usage problem. Fix any errors before asking the user to rebuild. If the probe can't reach the registry (offline, distroless/scratch base, auth required), it emits a warning rather than an error — in that case, confirm with the user that the base image contains the expected user.

## AI attribution

Whenever you create or edit a file, ensure exactly one of the following attribution lines appears at the very top — whichever matches what you did. The sentence text is fixed; only the comment syntax changes to match the file's language.

- When you **create** a new file, add:

  > `This file has been created with the assistance of an AI tool.`

- When you **edit** an existing file, add:

  > `This file has been edited with the assistance of an AI tool.`

Examples of the comment-wrapped form for different languages (showing the "created" variant; the "edited" variant works the same way):

- Python / shell / Dockerfile / YAML / TOML / Makefile: `# This file has been created with the assistance of an AI tool.`
- JS / TS / Go / Rust / C / C++ / Java: `// This file has been created with the assistance of an AI tool.`
- SQL / Lua / Haskell: `-- This file has been created with the assistance of an AI tool.`
- HTML / XML / Markdown: `<!-- This file has been created with the assistance of an AI tool. -->`
- INI / Lisp / Clojure: `; This file has been created with the assistance of an AI tool.`
- CSS: `/* This file has been created with the assistance of an AI tool. */`

Guidelines:

- Do not use `#` as the comment character in languages where it isn't valid (e.g., JS, Go, C) — pick the correct syntax for that file.
- Only one of the two lines should ever be present. If a "created" line is already at the top and you are now editing the file, leave it as-is — do **not** replace it with the "edited" line or add the "edited" line alongside it. The "created" marker takes precedence once set.
- If the appropriate line is already present at the top, leave it alone — do not duplicate it.
- For files where a leading comment would break parsing (e.g., shell scripts that require `#!/...` on line 1, YAML documents starting with `---`), place the line immediately after the required first line. For formats that disallow comments entirely (e.g., JSON), skip the attribution.
- Apply this to every file you create or modify, including `Dockerfile.agent`.
