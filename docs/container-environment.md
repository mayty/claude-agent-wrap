<!-- This file has been created with the assistance of an AI tool. -->
# Container Environment Variables

These vars are set by the wrapper on every `docker run`, regardless of provider (not baked into the image, so overriding them doesn't require a rebuild):

## Always-injected vars

| Var | Value |
| --- | --- |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |
| `AGENT_INSTANCE_ID` | `<agent-name>-<uuid>` — also the value of the `agent-wrap.instance-id` Docker label; the container itself is named `claude-agent-<agent-name>-<uuid>` |
| `AGENT_NAME` | from `# agent-name:` or sanitized project dir |
| `HOME` | `/home/<agent-user>` (default `/home/ubuntu`) |
| `TERM`, `COLORTERM` | forwarded from host shell, defaulting to `xterm-256color` / `truecolor` if unset |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | always passed (empty if not configured in `~/claude_keys.json`). The notification script checks that both are non-empty before sending. |

## Provider-injected vars

The active provider injects additional vars via `get_agent_env()` and `get_run_args()`. See the provider's README:

- [litellm-bedrock](../agent_wrap/providers/litellm_bedrock/README.md)
- [litellm-dashscope](../agent_wrap/providers/litellm_dashscope/README.md)
- [litellm-deepseek](../agent_wrap/providers/litellm_deepseek/README.md)

## Host-forwarded (conditional)

| Var | When forwarded | Effect |
| --- | --- | --- |
| `CLAUDE_CODE_ENABLE_AUTO_MODE` | Only when set in the host shell — forwarded verbatim (including `0`/empty) so you can both allow and explicitly disallow it. | Allows the use of Claude Code's [auto mode](https://code.claude.com/docs/en/auto-mode-config), an LLM-based permission classifier that auto-approves commands instead of prompting. This only matters on backends that **don't** speak the Anthropic protocol — i.e. the default `litellm-bedrock` provider, where auto mode is unavailable unless this var is set. The `litellm-dashscope` and `litellm-deepseek` providers use the Anthropic interface, which makes auto mode available by default, so the var is a no-op there. |

```sh
# Allow auto mode (LLM permission classifier) on Bedrock
CLAUDE_CODE_ENABLE_AUTO_MODE=1 agent run
```

## WSLg (conditional)

On WSL2+WSLg hosts, `DISPLAY` and `WAYLAND_DISPLAY` are forwarded from the host shell; `XDG_RUNTIME_DIR` is set to `/mnt/wslg/runtime-dir`. See [Clipboard / WSLg](wslg-clipboard.md).
