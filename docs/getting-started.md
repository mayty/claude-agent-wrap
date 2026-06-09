<!-- This file has been created with the assistance of an AI tool. -->
# Getting Started

## Requirements

- Docker
- API credentials for your chosen provider, stored in `~/claude_keys.json`. See [Providers](providers.md).
- (Optional) Telegram credentials for permission-request and stop notifications, added to the same `~/claude_keys.json`. See [Telegram notifications](telegram-notifications.md).

## Setup

Source the wrapper in your bash shell (add it to `~/.bashrc` to make it permanent):

```bash
source /path/to/claude-agent-wrap/agent-wrap.bashrc
```

Build the base image once:

```bash
agent rebuild
```

This creates a `claude-agent` image tagged with your host UID/GID so the container can write to mounted directories without permission issues.

## Usage

From any project directory, run:

```bash
agent run [claude-code-args...]
```

If a `Dockerfile.agent` exists in the current directory but you want to run against the base `claude-agent` image for this launch (e.g., to bypass a broken project image or compare behavior), pass `--base`:

```bash
agent run --base [claude-code-args...]
```
