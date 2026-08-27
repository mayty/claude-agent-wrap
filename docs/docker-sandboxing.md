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
| `git config --system --add safe.directory /workspace` | Lets git operate on `/workspace` despite the UID/GID remapping described in [Build Args](#build-args) |
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

`agent rebuild` always passes `--no-cache` to `docker build` — there is no Docker layer caching, so every `RUN` step re-executes on every rebuild.

## Validating `Dockerfile.agent`

Always run the validator after creating or editing `Dockerfile.agent`, before running `agent rebuild`:

```bash
/opt/agent-wrap/validate-dockerfile-agent              # validates ./Dockerfile.agent
/opt/agent-wrap/validate-dockerfile-agent path/to/file  # validates specific file
```

It uses the `hadolint`/`crane` tools baked into the base image to catch mistakes `docker build` alone won't — most importantly, base images that don't contain the expected user. Exit codes: `0` pass (warnings allowed), `1` errors, `2` file missing. Fix any errors before rebuilding. See [dockerfile-agent-guide.md](../ops/dockerfile-agent-guide.md) for the full validator contract.

### Example

See this project's own [Dockerfile.agent](../Dockerfile.agent), which installs the Python toolchain and `make` needed for development and testing, then copies `pyproject.toml` and installs this package itself in dev mode on top of the base image.

## Recognized Directives

`Dockerfile.agent` supports a few wrapper-specific comment directives in addition to normal Dockerfile syntax:

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`. Must match `[a-z0-9_.-]+` (Docker image names are lowercase).
- **`# agent-user: <username>`** — sets the in-container username (default `ubuntu`). The wrapper reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`. Only useful if the base image has been customized to run as a different user. Such an image must also create `/home/<username>/.cache/claude-cli-nodejs` owned by that user — Docker materializes missing bind-mount parents as `root:root`, which would leave the agent unable to write under `~/.cache`. The base image does this for `ubuntu` already; see [Volume Mounts](volume-mounts.md#per-project-state).
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each line is whitespace-split into tokens. Example:
  ```dockerfile
  # agent-run-args: --device /dev/fuse
  # agent-run-args: --cap-add SYS_ADMIN
  ```
  Mounts declared here get their host side prepared before launch — see
  [Mounts declared by a `Dockerfile.agent`](#mounts-declared-by-a-dockerfileagent) below.
- **`EXPOSE <port>`** — any standard `EXPOSE` directives cause the wrapper to publish those ports on `127.0.0.1`. A protocol suffix, if present, is stripped (`EXPOSE 8080/tcp` is published as `127.0.0.1:8080`).

## Mounts declared by a `Dockerfile.agent`

The flags in `# agent-run-args:` reach `docker run` exactly as written, but the wrapper reads any
mounts out of them and prepares the host side first, as the host user. Without that, Docker
materializes whatever is missing as `root:root` — an empty source directory the agent cannot write,
or, worse, a root-owned mountpoint inside your project.

- **Writable bind sources are created if missing.** `-v /srv/data:/data` creates `/srv/data`;
  `-v ./scratch:/scratch` creates `<project>/scratch`, because Docker resolves a relative source
  against the directory the launch runs from, which is the project. A source is created as a
  *directory* — the same thing Docker would have done — so a single-file bind mount must already
  exist on the host.
- **A missing read-only source fails the launch.** `:ro` says the content is expected to exist, so
  `agent run` reports the offending mount and exits 1 rather than mounting an empty directory whose
  emptiness only surfaces much later, inside the container.
- **Mountpoints under `/workspace` are created too.** `-v /workspace/node_modules` — the
  shadow-volume pattern — needs a `node_modules` directory inside the `/workspace` bind mount for
  the volume to land on; the wrapper creates it in the project as the host user. This holds for any
  mount targeting a path below `/workspace`, whatever its kind.

`-v`/`--volume`, `--mount type=bind` and `--tmpfs` are all understood. A source counts as a host
path when it starts with `/`, `./` or `../` — Docker's own rule for telling a path from a volume
name — so `cache:/cache` is left alone as a named volume. `~` and `$VAR` are **not** expanded (no
shell is involved), and the wrapper warns rather than rewriting what you wrote; use an absolute path
instead. See [Volume Mounts](volume-mounts.md#declared-by-a-dockerfileagent-conditional).

## Build Args

`agent rebuild` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. These are *available* build args: a `Dockerfile.agent` that needs them at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. The shipped base image does **not** bake in a per-host UID — it runs as the default `ubuntu` user. Per-user isolation happens at runtime: against a non-rootless Docker daemon, every `docker run` is invoked with `--user $(id -u):$(id -g)`, so the agent process matches the host user's UID/GID regardless of what is baked into the image. Under rootless Docker the flag is `--user 0:0` instead — the daemon maps container-root to the host user, which writes bind-mounted files as the host user and also overrides any non-root `USER` baked into an image (otherwise that user maps to an unprivileged subuid that cannot write host-owned mounts).

## Security Note

`agent-run-args` is a pass-through to `docker run`, so a third-party `Dockerfile.agent` can request `--privileged`, host bind mounts, etc. Audit comment lines as well as `RUN` instructions before building someone else's agent image.
