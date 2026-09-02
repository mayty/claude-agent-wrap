<!-- This file has been edited with the assistance of an AI tool. -->
# Getting Started

## Requirements

- Docker
- `curl` and `tar` — used once, by `bin/agent-bootstrap`, to fetch the CPython the wrapper runs on. **No system Python is needed:** the wrapper provisions its own pinned interpreter and never falls back to the host's `python3`, so it does not matter which Python (if any) your distro ships.
- Network access to PyPI on that same first run, for the wrapper's own third-party dependencies. On an air-gapped host, point pip at a local wheelhouse with its standard environment variables — `PIP_NO_INDEX=1` and `PIP_FIND_LINKS=/path/to/wheels`. The bootstrap reads neither; pip does, so no wrapper configuration is involved. The interpreter has an equivalent escape hatch in `AGENT_PYTHON_TARBALL`.
- API credentials for your chosen provider. The primary flow is the interactive prompt on the first `agent run` — the secret is required, so a TTY triggers a prompt when it's missing. `agent secrets set <provider>` sets it explicitly ahead of time. See [Providers](providers.md).
- (Optional) Telegram credentials for permission-request and stop notifications. Unlike provider credentials, Telegram secrets are optional and never trigger an interactive prompt — set them manually via `agent secrets set telegram` before they'll be picked up. See [Telegram notifications](telegram-notifications.md).

## Setup

Provision the pinned CPython and its dependencies (once per checkout, ~5 s):

```bash
/path/to/claude-agent-wrap/bin/agent-bootstrap
```

This downloads a [python-build-standalone](https://github.com/astral-sh/python-build-standalone) interpreter, verifies it against the SHA-256 in `python-pin.env`, and unpacks it into `.python/` inside the checkout. It then builds a venv on top of it and installs `bin/requirements.txt` — a fully pinned, hash-verified export of `uv.lock` — which is what `agent` actually runs on. `agent` refuses to run until this has happened, and tells you this command; there is deliberately no fallback to a system interpreter. `agent update` re-runs it for you whenever the pin or the dependencies move. To start over, `rm -rf .python` and run it again.

Nothing is ever replaced in place: each venv's directory name encodes the constraints it was built from, so a dependency change publishes a new one and leaves the old one usable. That also means `.python/` grows over time — `rm -rf .python && bin/agent-bootstrap` reclaims it.

### Working on agent-wrap itself

Contributors additionally need [uv](https://docs.astral.sh/uv/), which owns dependency resolution and backs `make dump-prod-constraints` and `make constraints-check`. It is not needed to *use* the wrapper. With it on `PATH`, one command replaces the bootstrap above:

```bash
make install
```

That is `bin/agent-bootstrap --dev`: it provisions the same pinned interpreter, then hands the whole dependency question to `uv sync --locked` — the prod dependencies **and** the dev group, out of the one lock `bin/requirements.txt` is exported from. So there is no second step and no reason to run the plain bootstrap first. It publishes its own venv, `venv-<ver>+<rel>-<target>-dev`, alongside any the plain bootstrap built; re-run it after any `uv lock`, and it re-syncs in place.

`make check` then runs the full QA suite. Working inside this project's own agent container, both `uv` and the dev group are already in the image — `agent rebuild` is enough.

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
