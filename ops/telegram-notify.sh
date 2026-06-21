#!/bin/bash
# This file has been edited with the assistance of an AI tool.
# telegram-notify.sh — Thin proxy that forwards Claude Code hook events to
# the Telegram decision sidecar. The sidecar owns all rendering, credentials,
# and Telegram Bot API interaction.
#
# Usage: telegram-notify.sh [stop|stopfailure]
#   stop         — Stop hook: fire-and-forget notification
#   stopfailure  — StopFailure hook: fire-and-forget notification
#   (default)    — PermissionRequest hook: held-open decision request
#
# The hook framework passes JSON on stdin. We forward it verbatim — the
# sidecar knows how to extract and render whatever it needs.
#
# All diagnostic output goes to stderr AND a persistent log file under
# /workspace/.claude/. Stdout is reserved for the hook framework's JSON
# response.

set -e

# --- helpers ---

LOG_FILE="${TELEGRAM_HOOK_LOG:-/workspace/.claude/telegram-hook.log}"
# Ensure the log directory exists (best-effort)
mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null || true

now() { date -u +"%Y-%m-%dT%H:%M:%SZ"; }

log() {
    local line
    line="[$(now)] ${AGENT_NAME:-?}/${AGENT_INSTANCE_ID:-?} $*"
    echo "$line" >&2
    echo "$line" >>"$LOG_FILE" 2>/dev/null || true
}

# Extract a summary from the hook stdin for logging — tool_name and a
# one-line hint about the input (command, file_path, etc.).
summarise() {
    echo "$HOOK_STDIN" | node -e "
        var j=JSON.parse(require('fs').readFileSync('/dev/stdin','utf8'));
        var ti=j.tool_input||{};
        var tn=j.tool_name||'';
        var hint='';
        if(ti.command) hint=ti.command.substring(0,80);
        else if(ti.file_path) hint=ti.file_path;
        else if(ti.pattern) hint=ti.pattern;
        else if(ti.url) hint=ti.url;
        else if(ti.plan) hint='plan ('+ti.plan.length+' chars)';
        else if(ti.questions) hint=ti.questions.length+' question(s)';
        else hint='<no detail>';
        console.log(tn+' ['+hint+']');
    " 2>/dev/null || echo '<unparseable>'
}

# --- signals ---

# The "terminal wins" race: if the user answers the terminal permission
# prompt before tapping a Telegram button, Claude sends SIGTERM to this
# process. The held /decision HTTP connection drops, and we exit cleanly
# so Claude proceeds with the terminal answer.
trap 'log "SIGTERM — terminal won, exiting"; echo "{}"; exit 0' TERM

# --- main ---

HOOK_STDIN=$(cat)
HOOK_MODE="${1:-permission}"
SUMMARY=$(summarise)

log "start mode=$HOOK_MODE tool=$SUMMARY"

# Gate: sidecar not available → exit silently (no Telegram configured)
if [ -z "${TELEGRAM_SIDECAR_URL:-}" ] || [ -z "${TELEGRAM_SIDECAR_TOKEN:-}" ]; then
    log "stop-reason=no-sidecar"
    echo '{}'
    exit 0
fi

AUTH="Authorization: Bearer ${TELEGRAM_SIDECAR_TOKEN}"
HOST="${TELEGRAM_SIDECAR_URL}"

case "$HOOK_MODE" in
    stop|stopfailure)
        # Fire-and-forget notification — forward raw stdin as-is.
        log "request POST $HOST/notify"
        http_code=$(curl -s -o /dev/null -w "%{http_code}" \
            --max-time 10 \
            -H "$AUTH" \
            -H "Content-Type: application/json" \
            -d "$HOOK_STDIN" \
            "$HOST/notify" 2>/dev/null || true)
        log "stop-reason=notified http-status=${http_code:-0}"
        ;;
    *)
        # PermissionRequest — held-open decision, raw stdin forwarded as-is.
        # Connection: close ensures clean socket teardown on SIGTERM.
        log "request POST $HOST/decision"
        response=$(curl -s -w "\n%{http_code}" \
            --max-time 540 \
            -H "$AUTH" \
            -H "Content-Type: application/json" \
            -H "Connection: close" \
            -d "$HOOK_STDIN" \
            "$HOST/decision" 2>/dev/null || true)

        # Split response: last line is the HTTP status code
        http_code=$(echo "$response" | tail -1)
        body=$(echo "$response" | sed '$d')

        if [ -n "$body" ]; then
            log "response http-status=$http_code body=$body"
            log "stop-reason=sidecar-response"
            # Sidecar owns the output format — echo its response verbatim.
            echo "$body"
            exit 0
        else
            log "stop-reason=no-response http-status=${http_code:-0}"
        fi
        # Fall through: no decision from phone → terminal prompt
        ;;
esac

log "stop-reason=terminal-fallback"
echo '{}'
exit 0
