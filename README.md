# claude-agent-wrap

A Docker-based wrapper for running the [Claude Code](https://github.com/anthropics/claude-code) CLI against AWS Bedrock. It packages Claude Code into a reproducible container image and exposes two bash functions — `agent` and `rebuild_agent` — that handle volume mounts, credentials, and per-project image customization.

## Why

Running Claude Code in a container isolates the tool from your host system, pins its dependencies, and lets different projects layer their own runtime requirements on top of a shared base image. The wrapper also routes the CLI through AWS Bedrock instead of the Anthropic API, so auth is an AWS bearer token rather than an Anthropic API key.

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

The wrapper mounts:

| Host | Container | Purpose |
| --- | --- | --- |
| `$(pwd)` | `/workspace` | Project files |
| `$(pwd)/.claude/sessions` | `/tmp/claude-home/.claude/projects/-workspace` | Per-project session history |
| `<wrap-dir>/.claude_config/claude.json` | `/tmp/claude-home/.claude.json` | Global Claude config |
| `<wrap-dir>/.claude_config/settings.json` | `/tmp/claude-home/.claude/settings.json` | Global Claude settings |

The container runs as your host user (`$(id -u):$(id -g)`). A `.claude/` directory is auto-created in each project and git-ignored.

## Per-project customization

To layer project-specific tooling on top of the base image, drop a `Dockerfile.agent` at the root of your project. The simplest way is to start from a copy of the base:

```bash
create_custom_agent   # copies Dockerfile → ./Dockerfile.agent, with a header
```

Then rebuild from inside that project:

```bash
rebuild_agent
```

The resulting image is tagged `claude-agent-<name>` and `agent` will pick it up automatically whenever you invoke it from that directory.

### Recognized directives

`Dockerfile.agent` supports a few wrapper-specific comment directives in addition to normal Dockerfile syntax:

- **`# agent-name: <name>`** (required) — names the image `claude-agent-<name>`. Must match `[a-zA-Z0-9_.-]+`.
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
| `agent [args...]` | Run Claude Code in a container against the resolved image for the current directory. |
| `rebuild_agent` | Rebuild the resolved image with `--no-cache`, passing `HOST_UID`/`HOST_GID`. |
| `create_custom_agent` | Scaffold a `Dockerfile.agent` in the current directory based on the base `Dockerfile`. |

## Environment

Two env vars are baked into the base image:

- `CLAUDE_CODE_USE_BEDROCK=1` — routes Claude Code through AWS Bedrock.
- `AWS_REGION=us-east-1` — default Bedrock region. Override in a `Dockerfile.agent` if needed.

The bearer token is injected at runtime as `AWS_BEARER_TOKEN_BEDROCK`, read from `~/claude_keys.json`.

## Layout

```
.
├── Dockerfile           # Base image: Ubuntu 24.04 + Node 24 + Claude Code CLI
├── agent-wrap.bashrc    # Shell functions: agent, rebuild_agent, create_custom_agent
├── CLAUDE.md            # Guidance for Claude Code when editing this repo
└── .claude_config/      # Global Claude config (git-ignored, auto-created)
```
