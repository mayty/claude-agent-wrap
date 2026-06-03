<!-- This file has been created with the assistance of an AI tool. -->
# Configuration

These environment variables affect wrapper behavior, not the container's environment.

## `AGENT_PROVIDER` (model-routing backend)

Selects which provider plugin to use. Each provider lives in `agent_wrap/providers/<name>/provider.py` and implements the [Provider ABC](../agent_wrap/providers/base.py). The default is `litellm-bedrock`, preserving historical behavior.

```sh
# Use the default LiteLLM-Bedrock provider (no var needed)
agent run

# Or pick a different one — the launcher fails fast and lists available
# providers if the directory doesn't exist.
AGENT_PROVIDER=my-direct-anthropic source agent-wrap.bashrc
agent run
```

Providers are auto-discovered by scanning `agent_wrap/providers/*/provider.py` for concrete `Provider` subclasses (`inspect.getmembers()` + `inspect.isabstract()`) — drop in a directory and it shows up in the error message above without any registry edits.

## `AGENT_USE_HOST_NETWORK` (WSL workaround)

Setting `AGENT_USE_HOST_NETWORK=1` (or any non-empty value other than `0`/`false`/`no`) makes `agent run` launch the container with `--network host`. The switch is honored only on WSL hosts (detected via `microsoft` in `/proc/version`); on macOS or native Linux it is ignored with a note.

Use this when you run multiple WSL2 distros that each have their own `dockerd`. All WSL2 distros share a single Linux kernel, so the two daemons fight over the kernel's iptables tables — specifically, the second daemon to start installs Docker's standard ruleset on `iptables-legacy`, which flips the legacy `FORWARD` chain policy from `ACCEPT` to `DROP`. Reply traffic to the first distro's existing containers then gets dropped before it reaches `docker0`. Symptom: parent shell stays online, but containers lose all outbound TCP (DNS UDP still works); recovery requires `wsl --shutdown`. Relaunching the container does not help, because the broken state is upstream of `docker0`.

`--network host` puts the agent in the WSL distro's namespace directly, sidestepping the bridge and the FORWARD chain entirely.

Trade-offs:

- The container loses network isolation from the WSL distro — services bind on the distro's interfaces, not on `docker0`.
- `EXPOSE` port mappings become meaningless and are skipped with a warning. Make in-container services bind to `127.0.0.1` (not `0.0.0.0`) to avoid LAN exposure, since there is no longer a `127.0.0.1:port:port` translation in front of them.
- If `Dockerfile.agent` already specifies `--network`/`--net` via `# agent-run-args:`, the env var is ignored with a warning (the project's explicit network choice wins).
- The flag also extends to any provider sidecar — when set on the **cold-start** launch, the sidecar is launched with `--network host` as well. First-launch-wins: subsequent launches without the flag adapt to the running mode rather than restarting it. To switch a running sidecar's mode, stop it and start the next launch with the desired flag value.

## `AGENT_SKIP_UPDATE_CHECK` (auto-update opt-out)

`agent run` and `agent rebuild` run a best-effort upstream check on every invocation: a `git fetch` against the wrap-dir's tracking branch, then — if `HEAD` is behind — a `Update agent-wrap now? [y/N]` prompt. On `y`, the wrapper runs `agent update` and returns without launching the container or rebuilding the image; re-source `agent-wrap.bashrc` and re-run your original command afterwards. On `n` (or Enter), the original command proceeds unchanged.

Set `AGENT_SKIP_UPDATE_CHECK=1` (or any non-empty value other than `0`/`false`/`no`) to disable the check entirely. The check is also auto-skipped on any error path — non-git wrap-dir, detached HEAD, fetch failure, or 10-second fetch timeout — so a flaky or offline network never blocks a launch.

Other verbs (`agent stats`, `agent create`, and `agent update` itself) do not perform the check.
