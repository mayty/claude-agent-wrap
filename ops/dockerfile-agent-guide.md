<!-- This file has been edited with the assistance of an AI tool. -->
# `Dockerfile.agent` Customization Guide

`Dockerfile.agent` carries directives that `agent-wrap` reads to customize the runtime. Use it instead of asking the user to change their shell invocation or installing dependencies ad-hoc inside the running container.

## Creating a new `Dockerfile.agent`

If none exists, create one at the project root:

```dockerfile
# agent-name: <host-project-directory-name>
FROM claude-agent

# project-specific RUN steps here
```

Set a literal name in the `# agent-name:` directive (lowercase, matching `[a-z0-9_.-]+`); it cannot reference a build-time variable. The launcher exposes the resolved name as the `$AGENT_NAME` env var at *runtime* inside the container, not during the image build. Do **not** use `workspace`: that's the in-container mount path, not the project name.

## Directives

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`.
- **`# agent-user: <username>`** — sets the container username (default `ubuntu`).
- **`# agent-run-args: <flags>`** — extra flags passed verbatim to `docker run`. Multiple lines allowed; each is whitespace-split into tokens.
- **`EXPOSE <port>`** — publishes ports on `127.0.0.1`.

**Build args:** the wrapper always passes `HOST_UID` and `HOST_GID` to `docker build`.
These are ordinary Dockerfile `ARG`s rather than wrapper directives — declare them with
`ARG HOST_UID` / `ARG HOST_GID` if your build needs to read them.

**Working directory:** must remain `WORKDIR /workspace` — do not change it.

## Security

Do not add `-v /var/run/docker.sock:/var/run/docker.sock`, `--privileged`, `--pid=host`, `--network=host`, or bind-mounts of sensitive host paths (`/`, `/etc`, `~/.ssh`, cloud credential dirs) unless the user explicitly requests it. Mounting the host Docker socket grants host-root-equivalent access.

## Shadow build/cache directories

Add anonymous volumes via `agent-run-args` to avoid polluting the host filesystem:

```dockerfile
# agent-run-args: -v /workspace/node_modules
# agent-run-args: -v /workspace/.venv
# agent-run-args: -v /workspace/target
```

## Validating `Dockerfile.agent`

**Always run `/opt/agent-wrap/validate-dockerfile-agent` after you create or edit `Dockerfile.agent`, before telling the user to run `agent rebuild`.** It catches mistakes that `docker build` alone won't — most importantly, base images that don't contain the expected user.

```sh
/opt/agent-wrap/validate-dockerfile-agent              # validates ./Dockerfile.agent
/opt/agent-wrap/validate-dockerfile-agent path/to/file  # validates specific file
```

Exit codes: `0` pass (warnings allowed), `1` errors, `2` file missing. Fix any errors before rebuild.

## Notes

The user can launch with `agent run --base` to bypass a project's `Dockerfile.agent` and run against the base `claude-agent` image instead. Project-specific `EXPOSE`, `agent-user`, and `agent-run-args` directives are skipped in this mode.

## Proactive suggestions

If you find that a tool isn't available in the current image but would be useful (even if not strictly required), propose adding it to `Dockerfile.agent` and let the user decide — don't add it silently or skip mentioning it.
