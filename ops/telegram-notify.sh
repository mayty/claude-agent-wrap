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
# process. We run curl in the background (instead of command substitution)
# so we can track its PID and kill it from the trap — this prevents
# orphaned HTTP connections to the sidecar.
CURL_PID=""
CURL_OUT=""

# Clean up temp file on any exit path.
cleanup_temp() {
    [ -n "$CURL_OUT" ] && rm -f "$CURL_OUT" 2>/dev/null || true
}

term_handler() {
    log "SIGTERM — terminal won, exiting"
    # Kill curl if still running (SIGKILL needed — curl may be blocked on TCP read)
    [ -n "$CURL_PID" ] && kill -9 "$CURL_PID" 2>/dev/null || true
    echo "{}"
    exit 0
}

trap term_handler TERM
trap cleanup_temp EXIT

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

# Temp file to capture curl output when run in background
CURL_OUT=$(mktemp) || { log "error=mktemp-failed"; echo "{}"; exit 1; }

case "$HOOK_MODE" in
    stop|stopfailure)
        # Fire-and-forget notification — forward raw stdin as-is.
        # Run curl in background so the SIGTERM trap can kill it.
        log "request POST $HOST/notify"
        curl -s -o /dev/null -w "%{http_code}" \
            --max-time 10 \
            -H "$AUTH" \
            -H "Content-Type: application/json" \
            -d "$HOOK_STDIN" \
            "$HOST/notify" >"$CURL_OUT" 2>/dev/null &
        CURL_PID=$!
        wait "$CURL_PID" 2>/dev/null || true
        CURL_PID=""
        http_code=$(cat "$CURL_OUT")
        log "stop-reason=notified http-status=${http_code:-0}"
        ;;
    *)
        # PermissionRequest — held-open decision, raw stdin forwarded as-is.
        # Run curl in background so the SIGTERM trap can kill it, preventing
        # orphaned connections on the "terminal wins" race.
        log "request POST $HOST/decision"
        curl -s -w "\n%{http_code}" \
            --max-time 540 \
            -H "$AUTH" \
            -H "Content-Type: application/json" \
            -H "Connection: close" \
            -d "$HOOK_STDIN" \
            "$HOST/decision" >"$CURL_OUT" 2>/dev/null &
        CURL_PID=$!
        wait "$CURL_PID" 2>/dev/null || true
        CURL_PID=""

        # Split response: last line is the HTTP status code
        response=$(cat "$CURL_OUT")
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
