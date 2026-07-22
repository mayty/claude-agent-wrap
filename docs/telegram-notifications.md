<!-- This file has been created with the assistance of an AI tool. -->
# Telegram Notifications

Claude Code can send you a Telegram message when it asks for permission to run a tool, finishes a response, or hits an API error. Useful if you step away mid-session.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and note the bot token.
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.
3. Set both secrets explicitly:
   ```bash
   agent secrets set telegram
   ```
   This prompts for `TelegramBotToken` and `TelegramChatId` and stores them encrypted. Telegram secrets are optional, so — unlike provider credentials — `agent run` never prompts for them interactively; they must be set this way before they'll be picked up.

Once both secrets are set, the next `agent run` launch idempotently injects three hook entries into `<wrap-dir>/.claude_config/.claude/settings.json` and starts a shared Telegram sidecar container. The sidecar manages the Bot API connection; the agent container receives opaque connectivity vars (`TELEGRAM_SIDECAR_URL`, `TELEGRAM_SIDECAR_TOKEN`) rather than the raw bot credentials. No `agent rebuild` needed — the [telegram-notify.sh](../ops/telegram-notify.sh) script is bind-mounted live and proxies events to the sidecar.

**Headless launches skip the sidecar.** When `agent run` is invoked with a flag that won't exercise the sidecar — `-p`/`--print` (non-interactive), or `--bare`/`--safe-mode` (hooks disabled) — the sidecar container is not started. It is still declared internally so that a headless run which happens to be the last agent out still tears down a sidecar a concurrent interactive run started.

## How it works

- **`PermissionRequest` hook** — fires when Claude asks to use a tool. Sends a tool-specific message (shell command with syntax highlighting for `Bash`, file paths for `Write`/`Edit`/`Read`, etc.).
- **`Stop` hook** — fires when Claude finishes its response. Sends the last assistant text (non-thinking content only).
- **`StopFailure` hook** — fires when the turn ends on an API error.

The hooks always run but only send a notification if `TELEGRAM_SIDECAR_URL` and `TELEGRAM_SIDECAR_TOKEN` are both non-empty in the container environment. The script returns `{}` and exits 0 on every path, so it never blocks Claude — even if the Telegram API is unreachable.
