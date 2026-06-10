<!-- This file has been edited with the assistance of an AI tool. -->
# claude-agent-wrap

A Docker-based wrapper for the Claude Code CLI that isolates the agent in containers, keeps API credentials out of the agent process (in the default provider), and lets each project customize its environment with a simple `Dockerfile.agent`.

It packages Claude Code into a reproducible container image and exposes a single bash function — `agent` — whose first argument is a verb (`run`, `rebuild`, `create`, `stats`, `logs`, `update`) that selects the operation. Volume mounts, credentials, and per-project image customization are handled automatically. Model traffic is routed through a provider plugin — all shipped providers use a [LiteLLM](https://github.com/BerriAI/litellm) sidecar. See [Providers](docs/providers.md) for available options.

## Documentation

| Category | Docs |
| --- | --- |
| Getting started | [Requirements + Setup](docs/getting-started.md) |
| Features | [Docker Sandboxing](docs/docker-sandboxing.md) · [Telegram Notifications](docs/telegram-notifications.md) · [WSLg Clipboard](docs/wslg-clipboard.md) |
| Providers | [Provider Setup](docs/providers.md) |
| Configuration | [Configuration](docs/configuration.md) |
| Reference | [Volume Mounts](docs/volume-mounts.md) · [Shell Commands](docs/shell-commands.md) · [Container Environment](docs/container-environment.md) |
| Changelog | [Release history](CHANGELOG.md) |

## Quick Start

Source the wrapper in your shell (add it to `~/.bashrc` to make it permanent):

```bash
source /path/to/claude-agent-wrap/agent-wrap.bashrc
```

Build the base image once:

```bash
agent rebuild
```

From any project directory, run:

```bash
agent run [claude-code-args...]
```

See the [Getting Started](docs/getting-started.md) guide for full setup instructions and [Providers](docs/providers.md) for your model-routing options.

## Project Layout

```
.
├── .agent-launches/      # Project registry and pricing cache (git-ignored)
├── .claude_config/       # Global Claude config (git-ignored)
├── agent_wrap/           # Python orchestration (commands, providers, config)
├── docs/                 # Documentation (linked from this file)
├── logs_page/            # Static web viewer served by `agent logs`
├── ops/                  # Base image, validator, status line, hooks, clipboard shim
├── scripts/              # Repo tooling (e.g. markdown-link validator)
└── agent-wrap.bashrc     # Shell function: `agent <verb>`
```
