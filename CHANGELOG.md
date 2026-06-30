<!-- This file has been created with the assistance of an AI tool. -->
# Changelog

Full release notes live per version under [`releases/`](releases/). Each entry below
links to the complete notes for that release, newest first. New notes should follow
the [release notes style guide](releases/styleguide.md).

## [0.7.0](releases/0.7.0.md) — 2026-06-30

Adds a Telegram decision sidecar so you can approve or deny Claude's tool-permission
prompts from your phone — Allow/Deny buttons right in the chat. Surfaces previously
invisible transient (`.agent_stats_leaf`) and `<orphaned>` projects in `agent stats` and
the `agent logs` viewer, and reworks `agent stats` with `--from`/`--until`/`--days`
windowing, more accurate cache-write cost, and a `--verbose` breakdown. Hardens the shared
LiteLLM sidecar for hundreds of parallel `agent run` jobs and turns `agent` into a
standalone executable. **Breaking:** custom provider forks must move to a single
`sidecars()` method, and `legacy_stats` is removed in favor of `agent stats`.

## [0.6.0](releases/0.6.0.md) — 2026-06-11

Fixes a cold-start crash where a fresh install aborted into Claude Code's configuration-error
prompt because `.claude.json` was seeded empty. Makes `agent logs` run the viewer in the
background (with a new `--stop` flag), and hardens the WSLg clipboard mount so it no longer
exposes the host filesystem inside the sandbox. Also adds this changelog, plus smaller `agent stats`
and Bedrock pricing fixes.

## [0.5.0](releases/0.5.0.md) — 2026-06-09

Request logging in the LiteLLM sidecar plus a new `agent logs` web viewer, a DeepSeek
provider, and `agent stats` rebuilt on the new logs. Docs overhauled into per-topic
guides. **Breaking:** the update opt-out env var was renamed to `AGENT_SKIP_UPDATE_CHECK`.

## [0.4.1](releases/0.4.1.md) — 2026-06-02

Pull the LiteLLM sidecar image automatically on first run when it isn't present locally.

## [0.4.0](releases/0.4.0.md) — 2026-06-01

Total Python rewrite of the orchestration layer behind a single `agent` command with
subcommands and tab completion, a DashScope provider, and a Python ABC provider plugin
system. **Breaking:** the old shell functions were renamed under the `agent` verb.

## [0.3.0](releases/0.3.0.md) — 2026-05-29

Model traffic now flows through a shared LiteLLM sidecar with a pluggable provider layer,
plus stronger per-project session isolation via dedicated `.claude/<subdir>/` mounts.

## [0.2.0](releases/0.2.0.md) — 2026-05-27

Best-effort auto-update check on each invocation and an `agent --base` flag to bypass a
project's `Dockerfile.agent` for a single launch.

## [0.1.0](releases/0.1.0.md) — 2026-05-26

Initial alpha release.
