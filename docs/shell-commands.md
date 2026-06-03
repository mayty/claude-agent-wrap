<!-- This file has been edited with the assistance of an AI tool. -->
# Shell Commands

`agent-wrap.bashrc` exposes a single shell function, `agent`, whose first argument is a verb that selects the operation. All verbs forward to `python3 -m agent_wrap` and run on the host.

| Verb | Purpose |
| --- | --- |
| `run` | Launch Claude Code in a container |
| `rebuild` | Rebuild the project or base image |
| `create` | Scaffold a `Dockerfile.agent` |
| `stats` | Aggregate token usage and cost |
| `update` | Pull latest wrapper source |

## `agent run`

```
agent run [--base] [claude-code-args...]
```

Launches Claude Code in a Docker container against the resolved image for the current directory. Records the project path in `<wrap-dir>/.agent-launches/projects.txt` for use by `agent stats`.

> This command and `agent rebuild` check for wrapper updates on every invocation. See [`AGENT_SKIP_UPDATE_CHECK`](configuration.md#agent_skip_update_check-auto-update-opt-out).

- **`--base`** — ignores any `Dockerfile.agent` in the current directory and launches the base `claude-agent` image instead. Project-specific `EXPOSE`, `agent-user`, and `agent-run-args` directives are skipped.

## `agent rebuild`

```
agent rebuild [--full]
```

Rebuilds the resolved image with `--no-cache`, passing `HOST_UID`/`HOST_GID` build args.

> This command and `agent run` check for wrapper updates on every invocation. See [`AGENT_SKIP_UPDATE_CHECK`](configuration.md#agent_skip_update_check-auto-update-opt-out).

- **`--full`** — rebuilds the base `claude-agent` image first, then the project image. Use this to update the pinned Claude Code CLI version (new releases come out daily), or when the base image is missing.

## `agent create`

```
agent create
```

Scaffolds a minimal `Dockerfile.agent` (`FROM claude-agent`) in the current directory.

## `agent stats`

```
agent stats [--days N] [--region LABEL] [--refresh]
```

Aggregates token usage and estimated USD cost across every project where you've launched `agent run`. Reads the project registry at `<wrap-dir>/.agent-launches/projects.txt` and walks each project's `.claude/sessions/*.jsonl` files. Pricing is fetched from AWS Bedrock and cached for 7 days.

- **`--days N`** — widens the per-day breakdown window (default 30; `0` = all active days).
- **`--region LABEL`** — overrides the pricing region (defaults to "US East (N. Virginia)").
- **`--refresh`** — forces a fresh pricing fetch, bypassing the cache.

## `agent update`

```
agent update
```

Pulls the latest wrapper source. On `master`, it only updates when a newer tag has been published and fast-forwards to that tag's commit; on any other branch it fast-forwards to the branch tip on any upstream commit. If `default-CLAUDE.md` changed, replaces the user's copy when unmodified or prompts when customized.
