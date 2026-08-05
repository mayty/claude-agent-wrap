<!-- This file has been created with the assistance of an AI tool. -->
# Getting Started

## Requirements

- Docker
- API credentials for your chosen provider. The primary flow is the interactive prompt on the first `agent run` — the secret is required, so a TTY triggers a prompt when it's missing. `agent secrets set <provider>` sets it explicitly ahead of time. See [Providers](providers.md).
- (Optional) Telegram credentials for permission-request and stop notifications. Unlike provider credentials, Telegram secrets are optional and never trigger an interactive prompt — set them manually via `agent secrets set telegram` before they'll be picked up. See [Telegram notifications](telegram-notifications.md).

## Setup

Source the wrapper in your bash shell (add it to `~/.bashrc` to make it permanent):

```bash
source /path/to/claude-agent-wrap/agent-wrap.bashrc
```

This adds `bin/agent` to your `PATH` and registers tab-completion. Programmatic callers that only need to launch `agent` can instead put `<repo>/bin` on `PATH` or symlink `bin/agent` into a directory already on `PATH` — no sourcing required. See [Shell Commands](shell-commands.md).

Build the base image:

```bash
agent rebuild --full
```

This creates a `claude-agent` image. Your host UID/GID are passed in as build args (`HOST_UID`/`HOST_GID`) so the container can write to mounted directories without permission issues.

## Usage

From any project directory, run:

```bash
agent run [claude-code-args...]
```

If a `Dockerfile.agent` exists in the current directory but you want to run against the base `claude-agent` image for this launch (e.g., to bypass a broken project image or compare behavior), pass `--base`:

```bash
agent run --base [claude-code-args...]
```
