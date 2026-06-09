<!-- This file has been edited with the assistance of an AI tool. -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Docker-based wrapper for running Claude Code CLI through multiple AI providers via a plugin system (AWS Bedrock, Alibaba Cloud DashScope, or DeepSeek). Model traffic is routed through a shared LiteLLM sidecar. See `docs/` for full documentation.

## Documentation guide

| Doc | When to read |
| --- | --- |
| [docs/getting-started.md](docs/getting-started.md) | Never — already handled on every `agent run` launch |
| [docs/docker-sandboxing.md](docs/docker-sandboxing.md) | Writing/editing a `Dockerfile.agent` or its directives |
| [docs/telegram-notifications.md](docs/telegram-notifications.md) | Debugging Telegram notification hooks |
| [docs/wslg-clipboard.md](docs/wslg-clipboard.md) | Debugging clipboard passthrough on WSL2 |
| [docs/providers.md](docs/providers.md) | Adding a new provider or switching providers |
| [docs/configuration.md](docs/configuration.md) | Debugging wrapper env vars like `AGENT_PROVIDER`, `AGENT_USE_HOST_NETWORK` |
| [docs/volume-mounts.md](docs/volume-mounts.md) | Debugging mount paths or per-project state persistence |
| [docs/shell-commands.md](docs/shell-commands.md) | Adding/editing an `agent` verb or its flags |
| [docs/container-environment.md](docs/container-environment.md) | Adding/editing container env var injection |
| [agent-wrap.bashrc](agent-wrap.bashrc) | Adding/editing shell completion or the `agent` function |
| [agent_wrap/providers/litellm_common/README.md](agent_wrap/providers/litellm_common/README.md) | Understanding the sidecar lifecycle or adding a LiteLLM provider |
| Provider READMEs (`agent_wrap/providers/*/README.md`) | Provider-specific env vars, credentials, or model mappings |

## Runtime contract

### Environment variables

See [docs/container-environment.md](docs/container-environment.md) for always-injected and conditional vars. Provider-specific vars are in each provider's README.

### Authentication

Credentials live in `~/claude_keys.json`. Key names are documented in each provider's README.

### Agent lifecycle

See [agent_wrap/providers/litellm_common/README.md](agent_wrap/providers/litellm_common/README.md).

## Per-project customization

See [docs/docker-sandboxing.md](docs/docker-sandboxing.md).

**Important:** do not change the working directory — it must remain `WORKDIR /workspace`.

## Keeping `default-CLAUDE.md` in sync

`default-CLAUDE.md` is copied into every consumer project's `.claude_config/.claude/CLAUDE.md` on first `agent run` and is how agents running in *other* projects learn about this wrapper's runtime contract.

**Update `default-CLAUDE.md` whenever you change wrapper behavior that a consumer agent needs to know about:** adding/removing directives, changing mount paths or environment assumptions, changing dependency installation rules, or changing persistence paths. No update needed for internal refactors, changes to this repo's own `CLAUDE.md`, or host-only changes.

## Development workflow

A `Makefile` provides all QA targets. Follow these rules:

- **`make check` must pass before handing off.** Never conclude a task until `make check` (lintcheck + format-check + test + typecheck + markdown-check) passes cleanly.
- **Prefer `make *` targets over running tools directly.** Use `make test`, `make lint`, `make format`, `make lintcheck`, `make typecheck`.
- **Fix lint/format errors with `make` first.** Auto-fix via `make lint` or `make format` before manual edits.
- **Never `pip install` dependencies.** Add them to the `dev` dependency group in `pyproject.toml` and prompt the user to run `agent rebuild`.

## Notes

- The Docker container runs as the current user (`$(id -u):$(id -g)`) to avoid permission issues.
- `HOME` inside the container is `/home/<agent-user>` (default `/home/ubuntu`); global Claude state lives under that path.
- The project `.claude` directory is automatically created and git-ignored.
