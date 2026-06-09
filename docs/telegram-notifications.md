<!-- This file has been created with the assistance of an AI tool. -->
# Telegram Notifications

Claude Code can send you a Telegram message when it asks for permission to run a tool, finishes a response, or hits an API error. Useful if you step away mid-session.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather) and note the bot token.
2. Get your chat ID by messaging [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID.
3. Add both to `~/claude_keys.json`:
   ```json
   {
     ...
     "TelegramBotToken": "11111111:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
     "TelegramChatId": "22222222"
   }
   ```

Once both `TelegramBotToken` and `TelegramChatId` are present in `~/claude_keys.json`, the next `agent run` launch idempotently injects three hook entries into `<wrap-dir>/.claude_config/.claude/settings.json` and forwards the credentials as env vars into the container. No `agent rebuild` needed — the [telegram-notify.sh](../ops/telegram-notify.sh) script and its [md_to_html.js](../ops/md_to_html.js) converter are bind-mounted live.

## How it works

- **`PermissionRequest` hook** — fires when Claude asks to use a tool. Sends a tool-specific message (shell command with syntax highlighting for `Bash`, file paths for `Write`/`Edit`/`Read`, etc.).
- **`Stop` hook** — fires when Claude finishes its response. Sends the last assistant text (non-thinking content only).
- **`StopFailure` hook** — fires when the turn ends on an API error.

The hooks always run but only send a notification if `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are both non-empty in the container environment. The script returns `{}` and exits 0 on every path, so it never blocks Claude — even if the Telegram API is unreachable.
