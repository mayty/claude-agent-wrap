<!-- This file has been edited with the assistance of an AI tool. -->
# Docker Sandboxing & Per-Project Customization

Running Claude Code in a container isolates the tool from your host system, pins its dependencies, and lets each project customize the base image with its own `.claude-agent-wrap/Dockerfile`.

## The base image

Every agent starts from `claude-agent`, built from [ops/Dockerfile](../ops/Dockerfile) in this repo. It provides:

| Component | Why |
| --- | --- |
| Ubuntu 24.04 | Clean, predictable base |
| Node.js 24.x | Required by Claude Code CLI |
| Claude Code CLI | Installed globally (`npm install -g @anthropic-ai/claude-code`) |
| `hadolint` + `crane` | Used by the project Dockerfile validator |
| `wl-clipboard`, `xclip`, `imagemagick` | WSLg clipboard passthrough |
| `curl`, `git`, `jq`, `file`, `ca-certificates` | Common utilities the agent may need |
| `WORKDIR /workspace` | Default working directory inside the container |
| `git config --system --add safe.directory /workspace` | Lets git operate on `/workspace` despite the UID/GID remapping described in [Build Args](#build-args) |
| `ENTRYPOINT ["claude"]` | The container runs Claude Code by default |

## When to customize

Most projects run `agent run` directly against the base image — no customization needed. Add a `.claude-agent-wrap/Dockerfile` when your project depends on tools the base image doesn't include, for example:

- Language runtimes (Python, Go, Rust, Java, etc.)
- Compilers or interpreters
- System libraries for native dependencies (libpq, libssl, etc.)
- Package managers for a specific ecosystem

## Project asset directory

Per-project wrapper assets live in `.claude-agent-wrap/` at the project root:

```text
.claude-agent-wrap/
├── Dockerfile     # the project image (optional)
└── startup.sh     # host-side pre-launch script (optional)
```

Unlike the git-ignored `.claude/` state tree beside it, `.claude-agent-wrap/` is **checked into the project** — it describes how the project is built and launched, so everyone working on it gets the same environment. A `.gitignore` entry of `.claude/` does not match it, but a looser `.claude*` would; check that if the directory unexpectedly fails to commit.

The Dockerfile is named plainly so editors, linters and syntax highlighters recognize the format. Note that the build **context is still the project root**, not `.claude-agent-wrap/`, so `COPY pyproject.toml …` works as written.

> **Deprecated:** the pre-0.10.0 location was `./Dockerfile.agent`. It still works, with a warning on every `agent run` and `agent rebuild`. If both files exist the wrapper refuses to run and asks you to delete the legacy one. Startup scripts are only honored in the new location.

## Quick start

Scaffold a minimal project Dockerfile:

```bash
agent create   # writes ./.claude-agent-wrap/Dockerfile
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

## Validating the project Dockerfile

Always run the validator after creating or editing the project Dockerfile, before running `agent rebuild`:

```bash
/opt/agent-wrap/validate-dockerfile-agent              # validates ./.claude-agent-wrap/Dockerfile
/opt/agent-wrap/validate-dockerfile-agent path/to/file  # validates specific file
```

With no argument it validates `.claude-agent-wrap/Dockerfile`, falling back to a deprecated `./Dockerfile.agent`.

It uses the `hadolint`/`crane` tools baked into the base image to catch mistakes `docker build` alone won't — most importantly, base images that don't contain the expected user. Exit codes: `0` pass (warnings allowed), `1` errors, `2` file missing. Fix any errors before rebuilding. See [dockerfile-agent-guide.md](../ops/dockerfile-agent-guide.md) for the full validator contract.

### Example

See this project's own [.claude-agent-wrap/Dockerfile](../.claude-agent-wrap/Dockerfile), which installs the Python toolchain and `make` needed for development and testing, then copies `pyproject.toml` and installs this package itself in dev mode on top of the base image.

## Recognized Directives

The project Dockerfile supports a few wrapper-specific comment directives in addition to normal Dockerfile syntax:

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`. Must match `[a-z0-9_.-]+` (Docker image names are lowercase).
- **`# agent-user: <username>`** — sets the in-container username (default `ubuntu`). The wrapper reroutes the global config mounts to `/home/<username>/.claude.json` and `/home/<username>/.claude/`. Only useful if the base image has been customized to run as a different user. Such an image must also create `/home/<username>/.cache/claude-cli-nodejs` owned by that user — Docker materializes missing bind-mount parents as `root:root`, which would leave the agent unable to write under `~/.cache`. The base image does this for `ubuntu` already; see [Volume Mounts](volume-mounts.md#per-project-state).
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each line is whitespace-split into tokens. Example:
  ```dockerfile
  # agent-run-args: --device /dev/fuse
  # agent-run-args: --cap-add SYS_ADMIN
  ```
  Mounts declared here get their host side prepared before launch — see
  [Mounts declared by the project Dockerfile](#mounts-declared-by-the-project-dockerfile) below.
- **`# agent-enable-startup: <value>`** — runs `.claude-agent-wrap/startup.sh` before launch (default off). See [Startup script](#startup-script) below. **Only honored in `.claude-agent-wrap/Dockerfile`** — in a deprecated `Dockerfile.agent` it is an error, not a silent skip.
- **`EXPOSE <port>`** — any standard `EXPOSE` directives cause the wrapper to publish those ports on `127.0.0.1`. A protocol suffix, if present, is stripped (`EXPOSE 8080/tcp` is published as `127.0.0.1:8080`).

## Startup script

Some projects need something to exist *on the host* before the container starts — most commonly a
Docker network the agent then joins. `.claude-agent-wrap/startup.sh` is run by `agent run` for
exactly that, once the directive enables it:

```dockerfile
# agent-name: myproj
# agent-enable-startup: true
# agent-run-args: --network myproj-net
FROM claude-agent
```

```bash
#!/usr/bin/env bash
# .claude-agent-wrap/startup.sh -- idempotent: it runs on every launch.
docker network inspect myproj-net >/dev/null 2>&1 || docker network create myproj-net
```

The contract:

- **Directive value.** `true`/`yes`/`on` enables the script with a **10-second** timeout;
  `false`/`no`/`off` (or no directive at all) disables it. A **positive number is a timeout in
  seconds** — `# agent-enable-startup: 45`. Numbers are always seconds, so `1` means one second,
  not "true"; the validator warns about that spelling. Anything else fails the launch.
- **Working directory** is the project root — the directory `agent run` was invoked from.
- **Interpreter** comes from the script's own shebang; with no shebang it runs under `/bin/sh`, so
  add `#!/usr/bin/env bash` if you use bash features. **No execute bit is required** (exec bits are
  routinely lost on Windows/WSL checkouts), and a CRLF shebang is tolerated. The `.sh` extension is
  conventional only — a shebang may select any interpreter.
- **Failure aborts the launch.** A non-zero exit, a timeout, or an unusable interpreter stops
  `agent run` before `docker run`; sidecars started for that launch are released. An agent whose
  prerequisites failed to materialize is worse than no agent.
- **The whole process tree is torn down.** The script runs in a session of its own, so a timeout
  signals the script *and everything it spawned*: `SIGTERM` to its process group, then `SIGKILL`
  five seconds later. Ctrl-C is relayed the same way and reported as a failed script. Nothing the
  script starts survives its budget, so it cannot be used to leave a daemon running — and a
  descendant that calls `setsid` for itself escapes the group and is out of reach.
- **stdin is closed.** The script cannot prompt — see the lock note below.
- **`agent run --base` skips it entirely**, along with every other project directive.
- **Timing.** It runs as late as possible in the pre-launch sequence: after the image is resolved,
  host mount paths are prepared, and sidecars are up — but still under the wrapper's startup lock,
  so two agents launching at once cannot race.

The environment it receives is the host environment plus:

| Variable | Value |
| --- | --- |
| `AGENT_NAME` | the project's `# agent-name:` |
| `AGENT_INSTANCE_ID` | this launch's unique instance id |
| `AGENT_SIDECAR_NETWORK` | the wrapper's own sidecar network (`agent-wrap-net`) |
| `AGENT_BINARY` | absolute path to the `agent` executable |

`AGENT_BINARY` lets the script call wrapper verbs without needing `agent` on `PATH`
(`"$AGENT_BINARY" inspect --lite`). **Read-only verbs only:** the script runs while holding the
host-global startup lock, so `agent run` from inside it would deadlock against that same lock until
the timeout kills it.

> **Keep it fast.** That lock is shared by every launching agent on the host, and the lock-wait
> budget does not account for script runtime — a slow script makes concurrent launches wait. That is
> why the default budget is 10 seconds. Prefer
> [`agent inspect --lite`](shell-commands.md#agent-inspect) over the full report here: it drops the
> npm-registry version check and the walk over the shared logs tree, which are the two things that
> can push `inspect` past a ten-second budget on their own.

If the script exists but nothing enables it, `agent run` warns rather than silently doing nothing.

## Mounts declared by the project Dockerfile

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
instead. See [Volume Mounts](volume-mounts.md#declared-by-the-project-dockerfile-conditional).

## Build Args

`agent rebuild` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. These are *available* build args: a project Dockerfile that needs them at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. The shipped base image does **not** bake in a per-host UID — it runs as the default `ubuntu` user. Per-user isolation happens at runtime: against a non-rootless Docker daemon, every `docker run` is invoked with `--user $(id -u):$(id -g)`, so the agent process matches the host user's UID/GID regardless of what is baked into the image. Under rootless Docker the flag is `--user 0:0` instead — the daemon maps container-root to the host user, which writes bind-mounted files as the host user and also overrides any non-root `USER` baked into an image (otherwise that user maps to an unprivileged subuid that cannot write host-owned mounts).

## Security Note

`agent-run-args` is a pass-through to `docker run`, so a third-party project Dockerfile can request `--privileged`, host bind mounts, etc. `agent-enable-startup` goes further: it runs a shell script from the repo on your host, outside the container, before the agent starts. Audit `.claude-agent-wrap/` in full — comment directives and `startup.sh` as well as `RUN` instructions — before building or launching someone else's agent.
