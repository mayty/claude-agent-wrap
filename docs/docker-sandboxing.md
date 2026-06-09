<!-- This file has been edited with the assistance of an AI tool. -->
# Docker Sandboxing & Per-Project Customization

Running Claude Code in a container isolates the tool from your host system, pins its dependencies, and lets each project customize the base image with its own `Dockerfile.agent`.

## The base image

Every agent starts from `claude-agent`, built from [ops/Dockerfile](../ops/Dockerfile) in this repo. It provides:

| Component | Why |
| --- | --- |
| Ubuntu 24.04 | Clean, predictable base |
| Node.js 24.x | Required by Claude Code CLI |
| Claude Code CLI | Installed globally (`npm install -g @anthropic-ai/claude-code`) |
| `hadolint` + `crane` | Used by the `Dockerfile.agent` validator |
| `wl-clipboard`, `xclip`, `imagemagick` | WSLg clipboard passthrough |
| `curl`, `git`, `jq`, `file`, `ca-certificates` | Common utilities the agent may need |
| `WORKDIR /workspace` | Default working directory inside the container |
| `ENTRYPOINT ["claude"]` | The container runs Claude Code by default |

## When to customize

Most projects run `agent run` directly against the base image — no customization needed. Add a `Dockerfile.agent` when your project depends on tools the base image doesn't include, for example:

- Language runtimes (Python, Go, Rust, Java, etc.)
- Compilers or interpreters
- System libraries for native dependencies (libpq, libssl, etc.)
- Package managers for a specific ecosystem

## Quick start

Scaffold a minimal `Dockerfile.agent` in your project root:

```bash
agent create   # writes a ./Dockerfile.agent
```

The generated stub:

```dockerfile
# agent-name: <derived-from-dirname>
FROM claude-agent

# Add project-specific RUN steps here.
```

Add your `RUN` steps below the `FROM`, then rebuild from inside that project:

```bash
agent rebuild
```

The resulting image is tagged `claude-agent-<name>` and `agent run` will pick it up automatically whenever you invoke it from that directory. There's no need to redeclare the base toolchain — it's inherited from `claude-agent` via `FROM`.

If the base `claude-agent` image hasn't been built yet on this host, run `agent rebuild --full` once — it builds the base first, then the project image.

### Example

See this project's own [Dockerfile.agent](../Dockerfile.agent), which installs the Python toolchain and `make` needed for development and testing on top of the base image.

## Recognized Directives

`Dockerfile.agent` supports a few wrapper-specific comment directives in addition to normal Dockerfile syntax:

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`. Must match `[a-z0-9_.-]+` (Docker image names are lowercase).
- **`# agent-user: <username>`** — sets the in-container username (default `ubuntu`). The wrapper reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`. Only useful if the base image has been customized to run as a different user.
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each line is whitespace-split into tokens. Example:
  ```dockerfile
  # agent-run-args: --device /dev/fuse
  # agent-run-args: --cap-add SYS_ADMIN
  ```
- **`EXPOSE <port>`** — any standard `EXPOSE` directives cause the wrapper to publish those ports on `127.0.0.1`. A protocol suffix, if present, is stripped (`EXPOSE 8080/tcp` is published as `127.0.0.1:8080`).

## Build Args

`agent rebuild` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. These are *available* build args: a `Dockerfile.agent` that needs them at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. The shipped base image does **not** bake in a per-host UID — it runs as the default `ubuntu` user. Per-user isolation happens at runtime: every `docker run` is invoked with `--user $(id -u):$(id -g)`, so the agent process matches the host user's UID/GID regardless of what is baked into the image.

## Security Note

`agent-run-args` is a pass-through to `docker run`, so a third-party `Dockerfile.agent` can request `--privileged`, host bind mounts, etc. Audit comment lines as well as `RUN` instructions before building someone else's agent image.
