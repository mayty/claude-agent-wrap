<!-- This file has been created with the assistance of an AI tool. -->
# Container Environment Variables

These vars are set by the wrapper on every `docker run`, regardless of provider (not baked into the image, so overriding them doesn't require a rebuild):

## Always-injected vars

| Var | Value |
| --- | --- |
| `AGENT_INSTANCE_ID` | `<agent-name>-<uuid>` — also the value of the `agent-wrap.instance-id` Docker label; the container itself is named `claude-agent-<agent-name>-<uuid>` |
| `AGENT_NAME` | from `# agent-name:` or sanitized project dir |
| `HOME` | `/home/<agent-user>` (default `/home/ubuntu`) |
| `TERM`, `COLORTERM` | forwarded from host shell, defaulting to `xterm-256color` / `truecolor` if unset |

## Provider-dependent vars

| Var | When set | Effect |
| --- | --- | --- |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` / `DISABLE_AUTOUPDATER` | `_build_env_args` picks one of the two, mutually exclusive, based on `Provider.disable_nonessential_traffic` (default `True`) | Every provider except `litellm-anthropic-sub` gets `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, which disables Datadog telemetry and feature-flag evaluation against Anthropic's backend. `litellm-anthropic-sub` opts out (`disable_nonessential_traffic = False`) because its users rely on that same evaluation for `/usage` and other Anthropic-backed feature checks; `_build_env_args` sets `DISABLE_AUTOUPDATER=1` for it instead, to keep the baked-in CLI from trying to self-update. |

## Provider-injected vars

The active provider injects additional vars via its `get_agent_env()`, plus the connectivity flags its sidecar(s) supply to the agent's `docker run`. See the provider's README:

- [litellm-anthropic-sub](../agent_wrap/domain/providers/litellm_anthropic_sub/README.md)
- [litellm-bedrock](../agent_wrap/domain/providers/litellm_bedrock/README.md)
- [litellm-dashscope](../agent_wrap/domain/providers/litellm_dashscope/README.md)
- [litellm-deepseek](../agent_wrap/domain/providers/litellm_deepseek/README.md)

Separately from `get_agent_env()`, the LiteLLM sidecar layer itself (`agent_wrap/domain/sidecars/litellm.py`) appends `ANTHROPIC_CUSTOM_HEADERS` (carrying the `x-agent-wrap-log-prefix` header used for per-project log routing) to the agent container's env — appends, because a provider may already have set entries of its own there, as `litellm-anthropic-sub` does. It also sets two vars on the **sidecar** container (never the agent): `AGENT_WRAP_PROVIDER`, which routes that sidecar's records into its own subtree of the shared request/response log, and `AGENT_WRAP_SIDECAR_PORT`, the port the sidecar resolved at start time — recorded so later launches on the same provider adopt it instead of scanning again. None of the three is declared by a provider's own `get_agent_env()`; they're injected by the common sidecar wiring that every LiteLLM-based provider goes through.

When the optional Telegram sidecar is active, it similarly injects `TELEGRAM_SIDECAR_URL` (and `TELEGRAM_SIDECAR_TOKEN`, when available) into the agent container. See [Telegram Notifications](telegram-notifications.md).

## Host-forwarded (conditional)

| Var | When forwarded | Effect |
| --- | --- | --- |
| `ENABLE_PROMPT_CACHING_1H` | Only when set in the host shell — forwarded verbatim (including `0`/empty) so you can both allow and explicitly disallow it. | Opts Claude Code into 1-hour prompt cache TTLs instead of the default 5-minute window, which can lower cost on long-running sessions. |
| `AGENT_TIMEZONE` | Only when set in the host shell. | An IANA zone name (e.g. `Europe/Warsaw`) the bundled statusline reads to show the `litellm-anthropic-sub` subscription "resets at HH:MM" time in that zone instead of the container's own local time. Also used host-side for the stats day boundary — see [`AGENT_TIMEZONE`](configuration.md#agent_timezone-display-timezone). |

```sh
# Opt into 1-hour prompt caching
ENABLE_PROMPT_CACHING_1H=1 agent run
```

## WSLg (conditional)

On WSL2+WSLg hosts, `DISPLAY` and `WAYLAND_DISPLAY` are forwarded from the host shell; `XDG_RUNTIME_DIR` is set to `/mnt/wslg/runtime-dir`. The same `/mnt/wslg`-directory check that gates these vars also gates the `wl-paste-shim` mount described in [Volume Mounts](volume-mounts.md) — both fire together. See [Clipboard / WSLg](wslg-clipboard.md).

## Injected settings (not env vars)

Two entries are written into the wrapper-global `<wrap-dir>/.claude_config/.claude/settings.json` on launch, both idempotently and both skipped if the file holds malformed JSON:

- **`statusLine`** — points at `/opt/agent-wrap/statusline.py`, the bundled two-line status line. It shows the model on one line and the remaining context plus the session id on the other, against today's token usage and an available-update notice on the right. Under `litellm-anthropic-sub` the token-usage segment is replaced by the five-hour subscription rate-limit window, whose reset time honours [`AGENT_TIMEZONE`](#host-forwarded-conditional).
- **Telegram hooks** — three entries pointing at `/opt/agent-wrap/telegram-notify.sh`, added only once the Telegram secrets are set. See [Telegram Notifications](telegram-notifications.md).
