# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository provides a Docker-based wrapper for running Claude Code CLI through AWS Bedrock. It packages Claude Code into a container and provides bash functions for easy invocation.

## Architecture

- **Dockerfile**: Builds an Ubuntu 24.04-based image with Node.js 24.x and Claude Code CLI installed globally. Configured to use AWS Bedrock for Claude API access.
- **agent-wrap.bashrc**: Provides bash functions to be sourced in your shell:
  - `agent()`: Runs Claude Code in Docker with proper volume mounts and credentials
  - `rebuild_agent()`: Rebuilds the Docker image with --no-cache

## Key Configuration

### Environment Variables (in Dockerfile)
- `CLAUDE_CODE_USE_BEDROCK=1`: Enables AWS Bedrock integration
- `AWS_REGION=us-east-1`: Default AWS region

### Volume Mounts (in agent function)
- Global config: `~/.claude_config/` → `/tmp/claude-home/.claude/`
- Project workspace: `$(pwd)` → `/workspace`
- Session storage: `.claude/sessions/` → `/tmp/claude-home/.claude/projects/-workspace`

### Authentication
The `agent()` function expects credentials in `~/claude_keys.json` with the structure:
```json
{
  "ServiceSpecificCredential": {
    "ServiceCredentialSecret": "your-aws-bearer-token"
  }
}
```

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

A project can provide its own `Dockerfile.agent` at its root to override the base image. The file must start with a `# agent-name: <name>` comment; the built image is tagged `claude-agent-<name>`. Two additional directives are recognized:

### `# agent-run-args: <flags>`

Extra flags passed through verbatim to `docker run`. Multiple lines are allowed; each line is whitespace-split into tokens (no shell quoting — args containing spaces cannot be expressed). Example:

```dockerfile
# agent-run-args: --device /dev/fuse --cap-add SYS_ADMIN
```

Security note: these flags are pass-through to `docker run`, so a `Dockerfile.agent` can request `--privileged`, host mounts, etc. Review comment lines as well as `RUN` instructions when auditing a third-party `Dockerfile.agent`.

### `HOST_UID` / `HOST_GID` build args

`rebuild_agent` always passes `--build-arg HOST_UID=$(id -u) --build-arg HOST_GID=$(id -g)`. A `Dockerfile.agent` that needs host-UID awareness at build time (e.g., to create a matching `/etc/passwd` entry or `chown` a directory) can declare `ARG HOST_UID` / `ARG HOST_GID` and consume them. Projects that don't use these args are unaffected — the base `Dockerfile` declares them as no-ops to silence Docker's unused-build-arg warning.

Because the baked-in UID differs per host user, each user on a shared host builds their own image variant under the same tag.

## Notes

- The Docker container runs as the current user (`$(id -u):$(id -g)`) to avoid permission issues
- Claude home directory is set to `/tmp/claude-home` inside the container
- The project `.claude` directory is automatically created and git-ignored by the wrapper script
