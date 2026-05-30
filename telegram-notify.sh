#!/bin/bash
# This file has been created with the assistance of an AI tool.
# telegram-notify.sh — Sends a Telegram notification for PermissionRequest, Stop, and StopFailure hooks.
# Usage: telegram-notify.sh [stop|stopfailure]
#   stop         — Stop hook: send last assistant message
#   stopfailure  — StopFailure hook: notify that Claude hit an API error
#   (default)    — PermissionRequest hook: send tool-specific details
#
# The hook framework passes JSON on stdin. We extract fields to include
# in the notification message.

set -e

# Read stdin once
HOOK_STDIN=$(cat)

# Use node to parse JSON — always available in the container
TOOL_NAME=$(echo "$HOOK_STDIN" | node -e "var d=require('fs').readFileSync('/dev/stdin','utf8'); var j=JSON.parse(d); console.log(j.tool_name||'')" 2>/dev/null || true)

# The Stop hook's JSON input carries the final assistant text directly as
# `last_assistant_message`. Prefer it over parsing the transcript: the
# transcript file is flushed *after* the hook fires, so reading it here
# returns the previous turn's text.
LAST_MSG=$(echo "$HOOK_STDIN" | node -e "var d=require('fs').readFileSync('/dev/stdin','utf8'); var j=JSON.parse(d); console.log(j.last_assistant_message||'')" 2>/dev/null || true)

# Convert markdown to Telegram HTML
md_to_html() {
    node /opt/agent-wrap/md_to_html.js "$1" 2>/dev/null || echo "$1"
}

# Escape a string for HTML (< > &)
html_escape() {
    echo "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'
}

# Build a rich PermissionRequest message based on tool type
build_permission_message() {
    local tool_name="$1"
    case "$tool_name" in
        Bash)
            local cmd desc
            cmd=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.command||'');
            " 2>/dev/null || true)
            desc=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.description||'');
            " 2>/dev/null || true)
            if [ -n "$cmd" ]; then
                local cmd_esc desc_html
                cmd_esc=$(html_escape "$cmd")
                if [ -n "$desc" ]; then
                    desc_html=$(md_to_html "$desc")
                    echo "⏳ <b>Permission request</b> — Shell command

${desc_html}

<pre language=\"shell\">$cmd_esc</pre>"
                else
                    echo "⏳ <b>Permission request</b> — Shell command

<pre language=\"shell\">$cmd_esc</pre>"
                fi
            else
                echo "⏳ <b>Permission request</b> — Shell"
            fi
            ;;
        Write)
            local fpath
            fpath=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.file_path||'');
            " 2>/dev/null || true)
            local fpath_esc
            fpath_esc=$(html_escape "$fpath")
            echo "⏳ <b>Permission request</b> — Write

<code>$fpath_esc</code>"
            ;;
        Edit)
            local fpath
            fpath=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.file_path||'');
            " 2>/dev/null || true)
            local fpath_esc
            fpath_esc=$(html_escape "$fpath")
            echo "⏳ <b>Permission request</b> — Edit

<code>$fpath_esc</code>"
            ;;
        Read)
            local fpath
            fpath=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.file_path||'');
            " 2>/dev/null || true)
            local fpath_esc
            fpath_esc=$(html_escape "$fpath")
            echo "⏳ <b>Permission request</b> — Read

<code>$fpath_esc</code>"
            ;;
        Grep)
            local pattern fpath
            pattern=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.pattern||'');
            " 2>/dev/null || true)
            fpath=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.path||'');
            " 2>/dev/null || true)
            local pat_esc path_esc
            pat_esc=$(html_escape "$pattern")
            path_esc=$(html_escape "$fpath")
            if [ -n "$fpath" ]; then
                echo "⏳ <b>Permission request</b> — Grep

<code>$pat_esc</code> in <code>$path_esc</code>"
            else
                echo "⏳ <b>Permission request</b> — Grep

<code>$pat_esc</code>"
            fi
            ;;
        Glob)
            local fpattern fpath
            fpattern=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.pattern||'');
            " 2>/dev/null || true)
            fpath=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.path||'');
            " 2>/dev/null || true)
            local pat_esc path_esc
            pat_esc=$(html_escape "$fpattern")
            path_esc=$(html_escape "$fpath")
            if [ -n "$fpath" ]; then
                echo "⏳ <b>Permission request</b> — Glob

<code>$pat_esc</code> in <code>$path_esc</code>"
            else
                echo "⏳ <b>Permission request</b> — Glob

<code>$pat_esc</code>"
            fi
            ;;
        WebFetch)
            local url prompt
            url=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.url||'');
            " 2>/dev/null || true)
            prompt=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                console.log(ti.prompt||'');
            " 2>/dev/null || true)
            local url_esc prompt_html
            url_esc=$(html_escape "$url")
            if [ -n "$prompt" ]; then
                prompt_html=$(md_to_html "$prompt")
                echo "⏳ <b>Permission request</b> — WebFetch

<code>$url_esc</code>

${prompt_html}"
            else
                echo "⏳ <b>Permission request</b> — WebFetch

<code>$url_esc</code>"
            fi
            ;;
        ExitPlanMode)
            # ExitPlanMode tool_input has no summary/label field — only
            # `plan` (markdown), `planFilePath`, and optional `allowedPrompts`.
            # The plan body is rendered as-is via md→HTML (headers become
            # bold/underline/italic), and allowedPrompts are listed when present.
            local perms_line plan_md plan_html
            perms_line=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ap=(j.tool_input||{}).allowedPrompts||[];
                function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
                if(Array.isArray(ap) && ap.length){
                    process.stdout.write(ap.map(function(p){
                        return '<code>'+esc(p.tool||'')+'</code>: '+esc(p.prompt||'');
                    }).join(', '));
                }
            " 2>/dev/null || true)
            plan_md=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var plan=(j.tool_input||{}).plan||'';
                // Truncate BEFORE md→HTML so we don't orphan tags. Telegram
                // caps at 4096; 3500 leaves headroom for header, permissions
                // line, agent-name line, and md→HTML tag expansion.
                if(plan.length>3500) plan=plan.substring(0,3500)+'\n\n…';
                process.stdout.write(plan);
            " 2>/dev/null || true)
            if [ -n "$plan_md" ]; then
                plan_html=$(md_to_html "$plan_md")
            else
                plan_html=""
            fi
            {
                printf '📋 <b>Plan ready for review</b>'
                if [ -n "$perms_line" ]; then
                    printf '\n\nPermissions: %s' "$perms_line"
                fi
                if [ -n "$plan_html" ]; then
                    printf '\n\n%s' "$plan_html"
                fi
            }
            ;;
        AskUserQuestion)
            local body
            body=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var qs=(j.tool_input||{}).questions||[];
                function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
                var LIMIT=3800;
                function render(withDesc){
                    return qs.map(function(q,i){
                        var head='<b>Q'+(i+1)+': '+esc(q.header||'')+'</b>';
                        var quest=esc(q.question||'');
                        var opts=(q.options||[]).map(function(o,k){
                            var letter=String.fromCharCode(97+k);
                            var line=letter+') <b>'+esc(o.label||'')+'</b>';
                            if(withDesc && o.description){
                                line+=' — '+esc(o.description);
                            }
                            return line;
                        }).join('\n');
                        return head+'\n'+quest+(opts?'\n\n'+opts:'');
                    }).join('\n\n');
                }
                var out=render(true);
                if(out.length>LIMIT) out=render(false);
                if(out.length>LIMIT){
                    out=out.substring(0,LIMIT-1)+'…';
                    var opens=(out.match(/<b>/g)||[]).length;
                    var closes=(out.match(/<\/b>/g)||[]).length;
                    while(closes++<opens) out+='</b>';
                }
                console.log(out);
            " 2>/dev/null || true)
            if [ -n "$body" ]; then
                echo "❓ <b>Input needed</b>

${body}"
            else
                echo "❓ <b>Input needed</b>"
            fi
            ;;
        *)
            # MCP or custom tools: dump the full tool_input as a code block
            local input_json
            input_json=$(echo "$HOOK_STDIN" | node -e "
                var d=require('fs').readFileSync('/dev/stdin','utf8');
                var j=JSON.parse(d);
                var ti=j.tool_input||{};
                // Pretty-print but truncate at 3800 chars (Telegram hard cap
                // is 4096; leave headroom for the header, agent-name line, and
                // HTML-escape expansion).
                var s=JSON.stringify(ti,null,2);
                if(s.length>3800) s=s.substring(0,3800)+'…';
                console.log(s);
            " 2>/dev/null || true)
            if [ -n "$input_json" ] && [ "$input_json" != "{}" ]; then
                local input_esc
                input_esc=$(html_escape "$input_json")
                echo "⏳ <b>Permission request</b> — $tool_name

<pre>$input_esc</pre>"
            else
                echo "⏳ <b>Permission request</b> — $tool_name"
            fi
            ;;
    esac
}

send_notification() {
    local msg="$1"
    if [ -n "${AGENT_NAME:-}" ]; then
        local name_esc
        name_esc=$(html_escape "$AGENT_NAME")
        if [[ "$msg" == *$'\n'* ]]; then
            msg="${msg/$'\n'/$'\n'<i>$name_esc</i>$'\n'}"
        else
            msg="${msg}"$'\n'"<i>$name_esc</i>"
        fi
    fi
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
            --data-urlencode "text=${msg}" \
            --data-urlencode "parse_mode=HTML" >/dev/null 2>&1 || true
    fi
}

case "${1:-}" in
    stop)
        if [ -n "$LAST_MSG" ]; then
            LAST_MSG_HTML=$(md_to_html "$LAST_MSG")
            send_notification "🏃 <b>Claude is done</b>

${LAST_MSG_HTML}"
        else
            send_notification "🏃 <b>Claude is done</b>"
        fi
        ;;
    stopfailure)
        send_notification "❌ <b>Claude hit an API error</b>"
        ;;
    *)
        MSG=$(build_permission_message "$TOOL_NAME")
        send_notification "$MSG"
        ;;
esac

echo '{}'
exit 0
