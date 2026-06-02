<!-- This file has been created with the assistance of an AI tool. -->
# Container Environment Variables

These vars are set by the wrapper on every `docker run`, regardless of provider (not baked into the image, so overriding them doesn't require a rebuild):

## Always-injected vars

| Var | Value |
| --- | --- |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` |
| `AGENT_INSTANCE_ID` | `<agent-name>-<uuid>` (also container name + Docker label) |
| `AGENT_NAME` | from `# agent-name:` or sanitized project dir |
| `HOME` | `/home/<agent-user>` (default `/home/ubuntu`) |
| `TERM`, `COLORTERM` | forwarded from host shell |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | always passed (empty if not configured in `~/claude_keys.json`). The notification script checks that both are non-empty before sending. |

## Provider-injected vars

The active provider injects additional vars via `get_agent_env()` and `get_run_args()`. See the provider's README:

- [litellm-bedrock](../agent_wrap/providers/litellm_bedrock/README.md)
- [litellm-dashscope](../agent_wrap/providers/litellm_dashscope/README.md)
- [litellm-deepseek](../agent_wrap/providers/litellm_deepseek/README.md)

## WSLg (conditional)

On WSL2+WSLg hosts, `DISPLAY`, `WAYLAND_DISPLAY`, and `XDG_RUNTIME_DIR` are forwarded. See [Clipboard / WSLg](wslg-clipboard.md).
