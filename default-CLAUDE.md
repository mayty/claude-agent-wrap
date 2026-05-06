<!-- This file has been edited with the assistance of an AI tool. -->
# Global instructions

## Environment

You are running inside a Docker container managed by the `agent-wrap` tooling. The container is built from a `Dockerfile.agent` in the project root (or from the base `Dockerfile` if none exists). Filesystem changes inside the container are discarded when it exits — only `/workspace` and the Claude home directory persist.

**Important:** You always run as a non-root user inside the container and are never granted `sudo` access. Do not attempt to use `sudo` or assume root privileges. If a task requires elevated permissions, instruct the user to add the necessary `RUN` steps to their `Dockerfile.agent` instead.

## Installing dependencies

Do **not** install dependencies ad-hoc inside the running container (`apt-get install`, `pip install`, `npm install -g`, etc.). Those changes are thrown away as soon as the session ends.

Instead:

- **If `Dockerfile.agent` exists in the project root:** edit it to add the dependency (e.g., add a `RUN apt-get install -y <pkg>` line), then tell the user to run `rebuild_agent` and restart the session.
- **If there is no `Dockerfile.agent`:** tell the user to run `create_custom_agent` to scaffold one, then edit the newly created file and prompt them to run `rebuild_agent`.

Project-level (language) dependencies that belong in the project's own manifest (`package.json`, `requirements.txt`, `go.mod`, `Cargo.toml`, etc.) can be installed normally — those live in `/workspace` and persist.

**Note:** If a tool you need is unavailable in the environment, add it to `Dockerfile.agent` (and prompt the user to rebuild) rather than working around its absence — e.g., don't reach for `curl` when `wget` is missing, don't hand-roll JSON parsing because `jq` isn't installed, don't script with `sed`/`awk` when the right tool is a one-line install. The only exception is when the user explicitly asks for a workaround.

## Per-project customization via `Dockerfile.agent`

`Dockerfile.agent` is not just a place to install packages — it also carries directives that `agent-wrap` reads to customize the runtime. Use them instead of asking the user to change their shell invocation or the global tooling.

**Important:** The working directory must always be `WORKDIR /workspace`. Do not change it — the wrapper mounts the project to `/workspace` and the agent expects to operate from there.

Recognized directives (as special comments in `Dockerfile.agent`):

- **`# agent-name: <name>`** — required. Tags the built image as `claude-agent-<name>`. Must match `[a-zA-Z0-9_.-]+`. Don't change this casually — renaming it orphans the previously built image.
- **`# agent-user: <username>`** — sets the container username (default: `ubuntu`). This reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`, and sets `HOME` accordingly. Use this when your custom image creates a different user than the base `ubuntu` user. Make sure the user exists in your `Dockerfile.agent` (e.g., via `useradd` with `ARG HOST_UID`/`ARG HOST_GID`) and that `/home/<username>` is writable by that user.
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each is whitespace-split into tokens (no shell quoting, so args containing spaces cannot be expressed). Use this when the container needs extra capabilities, devices, or mounts. Examples:

  ```dockerfile
  # agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN
  # agent-run-args: -v /var/run/docker.sock:/var/run/docker.sock
  ```

- **`EXPOSE <port>`** — any `EXPOSE` line in `Dockerfile.agent` is automatically published to `127.0.0.1:<port>` on the host. Use this for dev servers, debuggers, etc. — don't ask the user to add `-p` flags manually.
- **`ARG HOST_UID` / `ARG HOST_GID`** — `rebuild_agent` always passes the host user's UID/GID as build args. Declare these in `Dockerfile.agent` if you need them at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory baked into the image).

When a user asks for something that a `docker run` flag would solve (mounting a path, exposing a port, adding a capability, passing a device), add the appropriate directive to `Dockerfile.agent` and prompt them to run `rebuild_agent` — do not tell them to edit `agent-wrap.bashrc` or their shell invocation.

Security note: `agent-run-args` is pass-through to `docker run`, so it can grant `--privileged`, host mounts, and similar. Only add flags you actually need, and flag to the user what you're granting and why.

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
