<!-- This file has been edited with the assistance of an AI tool. -->
# Getting Started

## Requirements

- Docker
- `curl` and `tar` — used once, by `bin/agent-bootstrap`, to fetch the CPython the wrapper runs on. **No system Python is needed:** the wrapper provisions its own pinned interpreter and never falls back to the host's `python3`, so it does not matter which Python (if any) your distro ships.
- API credentials for your chosen provider. The primary flow is the interactive prompt on the first `agent run` — the secret is required, so a TTY triggers a prompt when it's missing. `agent secrets set <provider>` sets it explicitly ahead of time. See [Providers](providers.md).
- (Optional) Telegram credentials for permission-request and stop notifications. Unlike provider credentials, Telegram secrets are optional and never trigger an interactive prompt — set them manually via `agent secrets set telegram` before they'll be picked up. See [Telegram notifications](telegram-notifications.md).

## Setup

Provision the pinned CPython (once per checkout, ~2 s):

```bash
/path/to/claude-agent-wrap/bin/agent-bootstrap
```

This downloads a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) interpreter, verifies it against the SHA-256 in `python-pin.env`, and unpacks it into `.python/` inside the checkout. `agent` refuses to run until this has happened, and tells you this command — there is deliberately no fallback to a system interpreter. `agent update` re-runs it for you whenever the pin moves. To start over, `rm -rf .python` and run it again.

Source the wrapper in your bash shell (add it to `~/.bashrc` to make it permanent):

```bash
source /path/to/claude-agent-wrap/agent-wrap.bashrc
```

This adds `bin/agent` to your `PATH` and registers tab-completion. Programmatic callers that only need to launch `agent` can instead put `<repo>/bin` on `PATH` or symlink `bin/agent` into a directory already on `PATH` — no sourcing required. See [Shell Commands](shell-commands.md).

## Usage

From any project directory, run:

```bash
agent run [claude-code-args...]
```

If a `.claude-agent-wrap/Dockerfile` exists in the current directory but you want to run against the base `claude-agent` image for this launch (e.g., to bypass a broken project image or compare behavior), pass `--base`:

```bash
agent run --base [claude-code-args...]
```

### Rebuilding an image

There is nothing to build up front. `agent run` builds the `claude-agent` base image on a host that has none, and rebuilds either image when the wrapper's own build recipe has moved past it — so the first launch on a new host is slow and the rest are not.

Two things it cannot see for you, and both are an [`agent rebuild`](shell-commands.md#agent-rebuild):

```bash
agent rebuild --full   # take a new Claude Code release
agent rebuild          # apply your own .claude-agent-wrap/Dockerfile edits
```

A new Claude Code release announces itself in the statusline while you work, bottom-right and in yellow: `↑ 2.0.51 available`. The CLI is installed into the image at build time, so `agent rebuild --full` is how you take it — it rebuilds the base image and then the project image on top. (`agent inspect` reports the same comparison on its image rows, for when you are not in a session.) The plain `agent rebuild` is for the other case: nothing hashes your project Dockerfile, so an edit to it reaches the image only through an explicit rebuild.
