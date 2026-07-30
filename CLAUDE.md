<!-- This file has been edited with the assistance of an AI tool. -->
# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Docker-based wrapper for running Claude Code CLI through multiple AI providers via a plugin system (AWS Bedrock, Alibaba Cloud DashScope, or DeepSeek). Model traffic is routed through a per-provider LiteLLM sidecar, so agents on different providers run concurrently. See `docs/` for full documentation.

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
| [docs/architecture.md](docs/architecture.md) | Understanding the codebase architecture |
| [docs/testing-conventions.md](docs/testing-conventions.md) | Writing or reviewing tests |
| [agent-wrap.bashrc](agent-wrap.bashrc) | Adding/editing shell completion or the `agent` function |
| [agent_wrap/domain/providers/README.md](agent_wrap/domain/providers/README.md) | Understanding the sidecar lifecycle or adding a LiteLLM provider |
| Provider READMEs (`agent_wrap/domain/providers/*/README.md`) | Provider-specific env vars, credentials, or model mappings |

## Runtime contract

### Environment variables

See [docs/container-environment.md](docs/container-environment.md) for always-injected and conditional vars. Provider-specific vars are in each provider's README.

### Authentication

Provider credentials are resolved via an encrypted secrets store. The primary flow is the interactive prompt on the first `agent run` — provider secrets are required, so a TTY triggers a prompt when one is missing. `agent secrets set/check/clear/cleanup <sidecar>` manages secrets explicitly (e.g. for headless/scripted setup). Telegram secrets are optional and never trigger an interactive prompt — they must be set manually via `agent secrets set telegram`. `~/claude_keys.json` is only a legacy path: any keys found there are migrated into the encrypted store once, then the file is deleted.

### Agent lifecycle

See [agent_wrap/domain/providers/README.md](agent_wrap/domain/providers/README.md).

## Per-project customization

See [docs/docker-sandboxing.md](docs/docker-sandboxing.md).

**Important:** do not change the working directory — it must remain `WORKDIR /workspace`.

## Keeping `default-CLAUDE.md` in sync

`ops/default-CLAUDE.md` is copied into every consumer project's `.claude_config/.claude/CLAUDE.md` on first `agent run` and is how agents running in *other* projects learn about this wrapper's runtime contract.

**Update `ops/default-CLAUDE.md` whenever you change wrapper behavior that a consumer agent needs to know about:** adding/removing directives, changing mount paths or environment assumptions, changing dependency installation rules, or changing persistence paths. No update needed for internal refactors, changes to this repo's own `CLAUDE.md`, or host-only changes.

## Development workflow

A `Makefile` provides all QA targets. Follow these rules:

- **`make check` must pass before handing off.** Never conclude a task until `make check` (lintcheck + format-check + test + typecheck + markdown-check + arch-check + check-executables) passes cleanly.
- **Prefer `make *` targets over running tools directly.** Use `make test`, `make lint`, `make format`, `make lintcheck`, `make typecheck`.
- **Fix lint/format errors with `make` first.** Auto-fix via `make lint` or `make format` before manual edits.
- **Never `pip install` dependencies.** Add them to the `dev` dependency group in `pyproject.toml` and prompt the user to run `agent rebuild`.
- **Never import a private (`_`-prefixed) name from another module.** If a name is
  intended for import outside its defining module, it must be public (no underscore).
  Ruff's `SLF001` only catches `obj._attr` access, not `from module import _name`,
  so this is enforced by convention — enforce it in code review and when writing code.
  If you find yourself writing `from foo import _bar`, rename `_bar` → `bar` in its
  source module instead.
- **`__init__.py` files must not re-export names from sibling modules.** Every
  consumer imports directly from the module that defines the name.
- **Module-level constants imported by more than one module belong in
  `agent_wrap/constants.py`.**
- **Data/type-carrying classes belong in ``models.py``.** Dataclasses, TypedDicts,
  enums, type aliases, and plain data-holding classes must be defined in an optional
  ``models.py`` within their domain subpackage — not scattered across service files.
- **Module-level constants belong in ``constants.py``.** All module-level constants
  (whether public or ``_``-prefixed) must be defined in an optional ``constants.py``
  within their domain subpackage, neighboring ``models.py``.
- **Domain service classes belong in ``service.py``.** Every domain service class
  must be defined in ``service.py`` — not named after the subpackage (e.g.
  ``build.py``), and not with a ``_service`` suffix (e.g. ``provider_service.py``).
  Each domain subpackage defines exactly one service class in its ``service.py``.
- **No pure-proxy service methods.** A service method that only delegates to another
  callable (including one that only injects constructor dependencies before
  forwarding) is forbidden. Inline the target's implementation into the service
  method and delete the original target. See architecture.md rule 9.
- **Namespace classes replace comment-separated function blocks.** Standalone
  functions that share a micro-domain must be grouped into a namespace class
  (``@staticmethod``-only, no instance state) instead of being divided by
  ``# --- Topic ---`` comment separators.

## Architecture

Domain-layer architecture rules and project structure — see [docs/architecture.md](docs/architecture.md).

## Test conventions

See [docs/testing-conventions.md](docs/testing-conventions.md).

## Notes

- The Docker container runs as the current user (`$(id -u):$(id -g)`) to avoid permission issues.
- `HOME` inside the container is `/home/<agent-user>` (default `/home/ubuntu`); global Claude state lives under that path.
- The project `.claude` directory is automatically created and git-ignored.
