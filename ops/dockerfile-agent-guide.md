<!-- This file has been edited with the assistance of an AI tool. -->
# Project Customization Guide (`.claude-agent-wrap/`)

Per-project `agent-wrap` assets live in `.claude-agent-wrap/` at the project root:

```text
.claude-agent-wrap/
├── Dockerfile     # directives + image customization (optional)
└── startup.sh     # host-side pre-launch script (optional)
```

`.claude-agent-wrap/Dockerfile` carries directives that `agent-wrap` reads to customize the runtime. Use it instead of asking the user to change their shell invocation or installing dependencies ad-hoc inside the running container. The directory is checked into the project — it is not state.

**Deprecated location:** a project may still carry `./Dockerfile.agent`. It works, with a warning on every launch; migrate it with `mkdir -p .claude-agent-wrap && git mv Dockerfile.agent .claude-agent-wrap/Dockerfile`. Never create both — the wrapper refuses to run. `# agent-enable-startup:` is rejected outright in the legacy location.

## Creating a new `.claude-agent-wrap/Dockerfile`

If none exists, create one (the build context is the project root, not this directory):

```dockerfile
# agent-name: <host-project-directory-name>
FROM claude-agent

# project-specific RUN steps here
```

The final `FROM` **must** be `claude-agent` — the wrapper refuses to build or launch a project image that inherits from anything else, because it tracks freshness by the base image ID it stamps on each build. Earlier stages of a multi-stage build may use any image, so `FROM golang:1.23 AS builder` followed by `FROM claude-agent` and a `COPY --from=builder` is fine.

Set a literal name in the `# agent-name:` directive (lowercase, matching `[a-z0-9_.-]+`); it cannot reference a build-time variable. The launcher exposes the resolved name as the `$AGENT_NAME` env var at *runtime* inside the container, not during the image build. Do **not** use `workspace`: that's the in-container mount path, not the project name.

## Directives

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`.
- **`# agent-user: <username>`** — sets the container username (default `ubuntu`). Such an image must also `mkdir -p /home/<username>/.cache/claude-cli-nodejs` and `chown` it to that user, or the MCP log bind mount will land on `root`-owned parent directories the agent cannot write.
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each is whitespace-split into tokens. Mounts declared here have their host side prepared for you — see [Host mounts](#host-mounts).
- **`# agent-enable-startup: <value>`** — runs `.claude-agent-wrap/startup.sh` on the host before launch; default off. `true`/`yes`/`on` gives a 10-second timeout, a positive number sets the timeout in seconds, `false`/`no`/`off` disables. See [Startup script](#startup-script). **New location only** — an error in a legacy `Dockerfile.agent`.
- **`EXPOSE <port>`** — publishes ports on `127.0.0.1`.

**Build args:** the wrapper always passes `HOST_UID` and `HOST_GID` to `docker build`.
These are ordinary Dockerfile `ARG`s rather than wrapper directives — declare them with
`ARG HOST_UID` / `ARG HOST_GID` if your build needs to read them.

**Working directory:** must remain `WORKDIR /workspace` — do not change it.

## Security

Do not add `-v /var/run/docker.sock:/var/run/docker.sock`, `--privileged`, `--pid=host`, `--network=host`, or bind-mounts of sensitive host paths (`/`, `/etc`, `~/.ssh`, cloud credential dirs) unless the user explicitly requests it. Mounting the host Docker socket grants host-root-equivalent access.

`startup.sh` runs on the **host**, outside the container, with the user's own privileges. Do not create or enable one unless the user asked for host-side setup, and never put anything in it beyond what they requested.

## Host mounts

Mounts you declare in `agent-run-args` reach `docker run` exactly as written, but the wrapper reads
them first and prepares the host side as the host user — otherwise Docker creates what is missing as
`root:root` and the agent cannot write it.

- A **writable** bind source is created if missing, always as a *directory*. A single-file bind
  mount (`-v /home/me/.gitconfig:/home/ubuntu/.gitconfig`) must already exist on the host.
- A **read-only** (`:ro`) bind source is never invented: if it does not exist, `agent run` fails
  with an error naming the mount. Mark a mount `:ro` when it must carry existing content.
- A source must start with `/`, `./` or `../` to count as a host path (`./x` resolves against the
  project directory, as Docker resolves it). `cache:/cache` is a named volume. **`~` and `$VAR` are
  never expanded** — no shell is involved — so write absolute paths.
- Anything targeting a path under `/workspace` also gets its mountpoint created inside the project.

The **container** side is yours to prepare — the wrapper cannot reach inside the image. If you mount
to a path the image does not already contain, create it in the Dockerfile and `chown` it to the agent
user:

```dockerfile
# agent-run-args: -v /srv/models:/opt/models
RUN mkdir -p /opt/models && chown -R ubuntu:ubuntu /opt/models
```

Skip that and Docker materializes what is missing as `root:root`, which bites in two ways: a
mountpoint's missing **parent** becomes a root-owned directory the agent cannot write around
(`-v /srv/x:/opt/models/x` leaves `/opt/models` root-owned), and a **volume** mounted at a path the
image lacks is initialized from that freshly created root-owned directory, so the volume itself is
unwritable. For a non-default `# agent-user:`, `chown` to that user — see the directive's note above.

## Shadow build/cache directories

Add anonymous volumes via `agent-run-args` to avoid polluting the host filesystem:

```dockerfile
# agent-run-args: -v /workspace/node_modules
# agent-run-args: -v /workspace/.venv
# agent-run-args: -v /workspace/target
```

The mountpoints these need inside `/workspace` are pre-created as the host user, so the project is
not left with root-owned `node_modules/` directories after a run.

## Startup script

`.claude-agent-wrap/startup.sh` runs on the host before every launch, once `# agent-enable-startup:` turns it on. Its purpose is host state the container then consumes — most commonly a Docker network the agent joins:

```dockerfile
# agent-name: myproj
# agent-enable-startup: true
# agent-run-args: --network myproj-net
FROM claude-agent
```

```bash
#!/usr/bin/env bash
# Runs on every launch, so it must be idempotent.
docker network inspect myproj-net >/dev/null 2>&1 || docker network create myproj-net
```

Rules to write it against:

- **Idempotent**, always: it runs on every `agent run`.
- **Fast.** The default budget is 10 seconds, and the script holds a host-global lock while it runs, so every other launching agent waits behind it. Raise the budget only when the work genuinely needs it (`# agent-enable-startup: 45`).
- **cwd is the project root.** The interpreter comes from the shebang; with none it runs under `/bin/sh`, so write `#!/usr/bin/env bash` if you use bash features. No execute bit is needed.
- **Non-interactive** — stdin is closed. It cannot prompt.
- **Any failure aborts the launch**: non-zero exit, timeout, or an unusable interpreter.
- **Nothing it starts outlives the budget.** The script runs in its own process group; on a timeout (or Ctrl-C) the whole group is signalled, `SIGTERM` then `SIGKILL`. Do not use the script to leave a background daemon behind — start what the container needs as a container.
- Env it receives, on top of the host environment: `AGENT_NAME`, `AGENT_INSTANCE_ID`, `AGENT_SIDECAR_NETWORK`, and `AGENT_BINARY` (absolute path to the `agent` executable). Call only read-only verbs through `AGENT_BINARY` — `agent run` from the script deadlocks against the lock the script is holding.

## Validating the project Dockerfile

**Always run `/opt/agent-wrap/validate-dockerfile-agent` after you create or edit `.claude-agent-wrap/Dockerfile`, before telling the user to run `agent rebuild`.** It catches mistakes that `docker build` alone won't — most importantly a final `FROM` that is not `claude-agent`, and an `# agent-user:` the base image does not provide. It also checks the `agent-enable-startup` value and whether the directive and `startup.sh` agree.

```sh
/opt/agent-wrap/validate-dockerfile-agent              # validates ./.claude-agent-wrap/Dockerfile
/opt/agent-wrap/validate-dockerfile-agent path/to/file  # validates specific file
```

Exit codes: `0` pass (warnings allowed), `1` errors, `2` no file to validate — either none exists, or both `.claude-agent-wrap/Dockerfile` and a legacy `Dockerfile.agent` do and the ambiguity has to be resolved first. Fix any errors before rebuild — `agent run` builds too, so an unfixed error blocks a launch as well.

## Notes

The user can launch with `agent run --base` to bypass a project's Dockerfile and run against the base `claude-agent` image instead. Project-specific `EXPOSE`, `agent-user`, `agent-run-args` and `agent-enable-startup` directives are all skipped in this mode, so the startup script does not run either.

## Proactive suggestions

If you find that a tool isn't available in the current image but would be useful (even if not strictly required), propose adding it to `.claude-agent-wrap/Dockerfile` and let the user decide — don't add it silently or skip mentioning it.
