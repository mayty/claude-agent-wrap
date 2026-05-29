# This file has been edited with the assistance of an AI tool.
if [ -z "${AGENT_WRAP_MOUNT:-}" ]; then
    readonly AGENT_WRAP_MOUNT="/opt/agent-wrap"
fi

# Source the model-routing provider plugin. Each provider lives in its own
# subdirectory under providers/ and exposes a narrow contract (3 functions +
# 1 output array) documented in providers/template/. Selection is by
# AGENT_PROVIDER env var; default `litellm-bedrock` preserves historical
# behavior. Forks that swap the proxy implementation should drop in their own
# providers/<name>/ directory rather than editing the launcher.
# shellcheck disable=SC1091
{
    _agent_wrap_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    : "${AGENT_PROVIDER:=litellm-bedrock}"
    # Reject path-traversal and other names that wouldn't be a valid
    # subdirectory under providers/. A simple charset check catches both
    # accidental typos (e.g. trailing whitespace, `foo bar`) and the
    # `../something` shape that would otherwise resolve outside the tree.
    if ! [[ "$AGENT_PROVIDER" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        echo "agent-wrap: invalid AGENT_PROVIDER='${AGENT_PROVIDER}' — must match [a-zA-Z0-9_-]+" >&2
        unset _agent_wrap_dir
        return 1 2>/dev/null || exit 1
    fi
    _agent_provider_file="${_agent_wrap_dir}/providers/${AGENT_PROVIDER}/provider.sh"
    if [ ! -r "$_agent_provider_file" ]; then
        echo "agent-wrap: provider '${AGENT_PROVIDER}' not found at ${_agent_provider_file}" >&2
        echo "Available providers:" >&2
        for _d in "${_agent_wrap_dir}/providers"/*/; do
            [ -r "${_d}provider.sh" ] && echo "  - $(basename "$_d")" >&2
        done
        unset _agent_wrap_dir _agent_provider_file _d
        return 1 2>/dev/null || exit 1
    fi
    source "$_agent_provider_file"
    unset _agent_wrap_dir _agent_provider_file _d
}

_agent_resolve_image() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local USE_BASE=0
    if [ "${1:-}" = "--base" ]; then
        USE_BASE=1
    fi

    if [ "$USE_BASE" = "0" ] && [ -f "$(pwd)/Dockerfile.agent" ]; then
        local DOCKERFILE="$(pwd)/Dockerfile.agent"
        local NAME=$(grep -oE '^#[[:space:]]*agent-name:[[:space:]]*\S+' "$DOCKERFILE" | head -n1 | sed -E 's/^#[[:space:]]*agent-name:[[:space:]]*//')
        if [ -z "$NAME" ]; then
            echo "Error: Dockerfile.agent must contain '# agent-name: <name>' comment" >&2
            return 1
        fi
        if ! [[ "$NAME" =~ ^[a-z0-9_.-]+$ ]]; then
            echo "Error: agent-name '$NAME' must match [a-z0-9_.-]+ (Docker image names are lowercase)" >&2
            return 1
        fi
        printf '%s\t%s\t%s\n' "claude-agent-$NAME" "$DOCKERFILE" "$(pwd)"
    else
        printf '%s\t%s\t%s\n' "claude-agent" "$TOOL_DIR/Dockerfile" "$TOOL_DIR"
    fi
}

_agent_sanitize_name() {
    # Lowercase, replace anything outside [a-z0-9_.-] with `-`, strip leading/
    # trailing dashes. Output suitable as a Docker image-name suffix.
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_.-' '-' | sed -E 's/-+$//; s/^-+//'
}

# Wrapper-wide UUID source. Linux exposes one in /proc cheaply; macOS doesn't,
# so fall back to uuidgen. Output is lowercase-hex with dashes. Lives in the
# launcher (not a provider) because every launch needs it to mint
# AGENT_INSTANCE_ID before any provider hook runs.
_agent_uuid() {
    if [ -r /proc/sys/kernel/random/uuid ]; then
        cat /proc/sys/kernel/random/uuid
    elif command -v uuidgen >/dev/null 2>&1; then
        uuidgen | tr '[:upper:]' '[:lower:]'
    else
        echo "agent-wrap: no UUID source (need /proc/sys/kernel/random/uuid or uuidgen)" >&2
        return 1
    fi
}

create_custom_agent() {
    local DST="$(pwd)/Dockerfile.agent"

    if [ -e "$DST" ]; then
        echo "Error: $DST already exists" >&2
        return 1
    fi
    local NAME
    NAME=$(_agent_sanitize_name "$(basename "$(pwd)")")
    if [ -z "$NAME" ]; then
        echo "Error: could not derive agent-name from directory '$(pwd)'" >&2
        return 1
    fi
    cat > "$DST" <<EOF
# agent-name: $NAME
# This file has been created with the assistance of an AI tool.
FROM claude-agent

# Add project-specific RUN steps here.
EOF
    echo "Created $DST with agent-name '$NAME' (FROM claude-agent)"
}

_agent_ensure_statusline() {
    # Inject a default statusLine entry into settings.json when the key is
    # absent. If the user removes the key, it is restored on the next launch
    # (by design — to customize, redefine the key rather than deleting).
    local SETTINGS="$1"
    [ -s "$SETTINGS" ] || echo '{}' > "$SETTINGS"
    if ! jq -e . "$SETTINGS" >/dev/null 2>&1; then
        # malformed JSON — don't clobber
        return 0
    fi
    if jq -e 'has("statusLine")' "$SETTINGS" >/dev/null; then
        return 0
    fi
    local TMP="${SETTINGS}.tmp"
    jq '. + {statusLine: {type: "command", command: "/opt/agent-wrap/statusline.py"}}' \
        "$SETTINGS" > "$TMP" && mv "$TMP" "$SETTINGS"
}

_agent_record_project() {
    # Append $(pwd) to the project registry if not already present. The
    # registry is a flat list of absolute paths used by `agent_usage` to
    # discover where the user has been launching `agent`. Failures are
    # non-fatal — the agent launch must not depend on this.
    local TOOL_DIR="$1"
    local DIR="$TOOL_DIR/.agent-launches"
    local FILE="$DIR/projects.txt"
    mkdir -p "$DIR" 2>/dev/null || return 0
    touch "$FILE" 2>/dev/null || return 0
    local CWD="$(pwd)"
    grep -Fxq "$CWD" "$FILE" 2>/dev/null || echo "$CWD" >> "$FILE" 2>/dev/null || true
}

_agent_ensure_telegram_hooks() {
    # Idempotently inject PermissionRequest/Stop/StopFailure hooks that invoke
    # telegram-notify.sh. Only called when Telegram creds are present so users
    # who don't opt in don't accumulate dead hook entries.
    local SETTINGS="$1"
    [ -s "$SETTINGS" ] || echo '{}' > "$SETTINGS"
    if ! jq -e . "$SETTINGS" >/dev/null 2>&1; then
        return 0
    fi
    local CMD="/opt/agent-wrap/telegram-notify.sh"
    local TMP="${SETTINGS}.tmp"
    jq --arg cmd "$CMD" '
        def ensure_hook(event; full_cmd):
            .hooks //= {}
            | .hooks[event] //= []
            | if any(.hooks[event][]?; (.hooks // []) | any(.command == full_cmd))
              then .
              else .hooks[event] += [{matcher: "", hooks: [{type: "command", command: full_cmd}]}]
              end;
        ensure_hook("PermissionRequest"; $cmd)
        | ensure_hook("Stop"; $cmd + " stop")
        | ensure_hook("StopFailure"; $cmd + " stopfailure")
    ' "$SETTINGS" > "$TMP" && mv "$TMP" "$SETTINGS"
}

_agent_check_for_updates() {
    # Best-effort upstream check: if the wrap-dir is behind origin/<branch>,
    # prompt the user to update. On accept, pull and re-source this file in
    # the parent shell, then return 1 to tell the caller to abort the
    # original command. Any error path (no network, detached HEAD, non-git
    # wrap-dir, fetch failure, etc.) returns 0 so the original command runs.
    case "${CLAUDE_AGENT_SKIP_UPDATE_CHECK:-}" in
        ""|0|false|FALSE|no|NO) ;;
        *) return 0 ;;
    esac
    local TOOL_DIR
    TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)" || return 0
    git -C "$TOOL_DIR" rev-parse --git-dir >/dev/null 2>&1 || return 0
    local BRANCH
    BRANCH=$(git -C "$TOOL_DIR" symbolic-ref --short HEAD 2>/dev/null) || return 0
    timeout 10s git -C "$TOOL_DIR" fetch --quiet origin "$BRANCH" >/dev/null 2>&1 || return 0
    local BEHIND
    BEHIND=$(git -C "$TOOL_DIR" rev-list --count "HEAD..origin/$BRANCH" 2>/dev/null) || return 0
    if [ -z "$BEHIND" ] || [ "$BEHIND" = "0" ]; then
        return 0
    fi
    local y=$'\033[1;33m' r=$'\033[0m'
    echo "${y}Note:${r} agent-wrap is $BEHIND commit(s) behind origin/$BRANCH."
    local ans
    read -r -p "Update agent-wrap now? [y/N] " ans
    case "$ans" in
        y|Y)
            agent-wrap_update || return 0
            # shellcheck disable=SC1091
            source "$TOOL_DIR/agent-wrap.bashrc"
            return 1
            ;;
        *) return 0 ;;
    esac
}

agent-wrap_update() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local BRANCH BEFORE AFTER
    BRANCH=$(git -C "$TOOL_DIR" symbolic-ref --short HEAD) || return 1
    BEFORE=$(git -C "$TOOL_DIR" rev-parse HEAD) || return 1

    local USER_CLAUDE_MD="$TOOL_DIR/.claude_config/.claude/CLAUDE.md"
    local DEFAULT_CLAUDE_MD="$TOOL_DIR/default-CLAUDE.md"
    local pre_state="missing"
    if [ -f "$USER_CLAUDE_MD" ]; then
        if diff -wB --strip-trailing-cr -q "$USER_CLAUDE_MD" "$DEFAULT_CLAUDE_MD" >/dev/null 2>&1; then
            pre_state="matches"
        else
            pre_state="customized"
        fi
    fi

    git -C "$TOOL_DIR" pull --ff-only origin "$BRANCH" || return 1
    AFTER=$(git -C "$TOOL_DIR" rev-parse HEAD) || return 1
    local y=$'\033[1;33m' r=$'\033[0m'
    echo ""
    if [ "$BEFORE" = "$AFTER" ]; then
        echo "${y}Note:${r} already up to date; no action needed."
        return 0
    fi

    echo "${y}Note:${r} restart your shell (or re-source agent-wrap.bashrc) to pick up script changes."
    echo "${y}Note:${r} run 'rebuild_agent' to rebuild the Docker image with the updated files."

    if git -C "$TOOL_DIR" diff --quiet "$BEFORE" "$AFTER" -- default-CLAUDE.md; then
        return 0
    fi

    case "$pre_state" in
        missing)
            ;;
        matches)
            rm -f "$USER_CLAUDE_MD"
            echo "${y}Note:${r} default-CLAUDE.md changed and your .claude_config/.claude/CLAUDE.md was unmodified; removed it so the next 'agent' run will install the new default."
            ;;
        customized)
            echo ""
            echo "${y}Warning:${r} default-CLAUDE.md changed upstream, but your .claude_config/.claude/CLAUDE.md has local customizations and was NOT touched."
            echo "To update it manually:"
            echo "  1. Review the upstream change:  git -C \"$TOOL_DIR\" diff $BEFORE $AFTER -- default-CLAUDE.md"
            echo "  2. Compare with your copy:      diff -u \"$USER_CLAUDE_MD\" \"$DEFAULT_CLAUDE_MD\""
            echo "  3. Either merge the changes into \"$USER_CLAUDE_MD\" by hand,"
            echo "     or delete it to accept the new default (losing your customizations):"
            echo "        rm \"$USER_CLAUDE_MD\""
            echo "     The next 'agent' run will then copy the new default into place."
            ;;
    esac
}

rebuild_agent() {
    local FULL=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --full) FULL=1; shift ;;
            -h|--help)
                echo "Usage: rebuild_agent [--full]"
                echo "  --full  Rebuild the base 'claude-agent' image first, then the project image."
                return 0 ;;
            *)
                echo "Error: unknown argument '$1' (expected --full)" >&2
                return 1 ;;
        esac
    done

    if ! _agent_check_for_updates; then
        return 0
    fi

    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local RESOLVED
    RESOLVED=$(_agent_resolve_image) || return 1
    local IMAGE DOCKERFILE CONTEXT
    IFS=$'\t' read -r IMAGE DOCKERFILE CONTEXT <<< "$RESOLVED"

    if [ "$FULL" = "1" ]; then
        echo "--- Building base claude-agent from $TOOL_DIR/Dockerfile ---"
        docker build --no-cache \
            --build-arg HOST_UID="$(id -u)" \
            --build-arg HOST_GID="$(id -g)" \
            -f "$TOOL_DIR/Dockerfile" -t "claude-agent" "$TOOL_DIR" || return 1

        if [ "$IMAGE" = "claude-agent" ]; then
            echo "--- No Dockerfile.agent in $(pwd); base build is the only build needed ---"
            docker images --filter "reference=$IMAGE"
            return 0
        fi
    fi

    if [ "$FULL" = "0" ] && [ "$IMAGE" != "claude-agent" ]; then
        local from_line
        from_line=$(grep -iE '^FROM[[:space:]]+' "$DOCKERFILE" | tail -n1 | awk '{print $2}')
        if [[ "$from_line" =~ ^claude-agent(:.*)?$ ]] \
           && ! docker image inspect claude-agent >/dev/null 2>&1; then
            echo "Error: '$DOCKERFILE' uses 'FROM claude-agent' but the base image is not built." >&2
            echo "       Run 'rebuild_agent --full' to build the base first." >&2
            return 1
        fi
        if [ -n "$from_line" ] && ! [[ "$from_line" =~ ^claude-agent(:.*)?$ ]]; then
            local y=$'\033[1;33m' r=$'\033[0m'
            echo "${y}Note:${r} '$DOCKERFILE' inherits from '$from_line' rather than 'claude-agent'. Consider migrating to 'FROM claude-agent' to reuse the base toolchain." >&2
        fi
    fi

    echo "--- Building $IMAGE from $DOCKERFILE ---"
    docker build --no-cache \
        --build-arg HOST_UID="$(id -u)" \
        --build-arg HOST_GID="$(id -g)" \
        -f "$DOCKERFILE" -t "$IMAGE" "$CONTEXT"
    docker images --filter "reference=$IMAGE"
}

agent_usage() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local REG="$TOOL_DIR/.agent-launches/projects.txt"
    if ! command -v python3 >/dev/null 2>&1; then
        echo "Error: python3 is required on the host to run agent_usage." >&2
        return 1
    fi
    if [ ! -f "$REG" ]; then
        echo "agent_usage: no projects recorded yet — launch 'agent' once to register a project." >&2
        return 0
    fi
    python3 "$TOOL_DIR/agent_usage.py" \
        --cache "$TOOL_DIR/.agent-launches/pricing.json" \
        "$@" "$REG"
}

agent() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local GLOBAL_CONFIG_DIR="$TOOL_DIR/.claude_config"
    local SECRETS_FILE="${HOME}/claude_keys.json"
    local AGENT_USER="ubuntu"

    local USE_BASE=0
    local CLAUDE_ARGS=()
    local arg
    for arg in "$@"; do
        if [ "$arg" = "--base" ]; then
            USE_BASE=1
        else
            CLAUDE_ARGS+=("$arg")
        fi
    done

    if ! _agent_check_for_updates; then
        return 0
    fi

    local RESOLVED
    if [ "$USE_BASE" = "1" ]; then
        RESOLVED=$(_agent_resolve_image --base) || return 1
    else
        RESOLVED=$(_agent_resolve_image) || return 1
    fi
    local IMAGE DOCKERFILE CONTEXT
    IFS=$'\t' read -r IMAGE DOCKERFILE CONTEXT <<< "$RESOLVED"

    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        if [ "$USE_BASE" = "1" ]; then
            echo "Error: Base image '$IMAGE' not found. Run 'rebuild_agent --full' to build it." >&2
        else
            echo "Error: Image '$IMAGE' not found. Run 'rebuild_agent' in this directory to build it." >&2
        fi
        return 1
    fi

    local USER_ARGS=()
    if ! docker info 2>/dev/null | grep -qi 'rootless'; then
        USER_ARGS=(--user "$(id -u):$(id -g)")
    fi

    # Shadow any user-exported PROVIDER_EXTRA_RUN_ARGS for the duration of
    # this launch. The provider's _provider_ensure is contractually expected
    # to populate this array, but a third-party provider that succeeds without
    # writing it would otherwise inherit whatever the user happened to have in
    # their shell — splicing those flags into the agent's `docker run`.
    local PROVIDER_EXTRA_RUN_ARGS=()

    local PORT_ARGS=()
    local EXTRA_RUN_ARGS=()
    if [[ "$DOCKERFILE" == */Dockerfile.agent ]]; then
        local expose_tokens
        expose_tokens=$(grep -iE '^EXPOSE[[:space:]]+' "$DOCKERFILE" | sed -E 's/^[Ee][Xx][Pp][Oo][Ss][Ee][[:space:]]+//')
        for token in $expose_tokens; do
            local port="${token%/*}"
            PORT_ARGS+=(-p "127.0.0.1:${port}:${token}")
        done

        local user_override
        user_override=$(grep -E '^#[[:space:]]*agent-user:[[:space:]]*\S+' "$DOCKERFILE" | head -n1 | sed -E 's/^#[[:space:]]*agent-user:[[:space:]]*//')
        if [ -n "$user_override" ]; then
            AGENT_USER="$user_override"
        fi

        while IFS= read -r line; do
            # shellcheck disable=SC2206
            EXTRA_RUN_ARGS+=(${line})
        done < <(grep -E '^#[[:space:]]*agent-run-args:[[:space:]]*.*' "$DOCKERFILE" \
                 | sed -E 's/^#[[:space:]]*agent-run-args:[[:space:]]*//')
    fi

    # Extract any project-supplied --network from agent-run-args. The sidecar
    # needs to know it so it can attach itself to that network and become
    # reachable from the agent by container name. First occurrence wins —
    # docker itself errors on duplicate --network flags, so a malformed
    # Dockerfile.agent surfaces via docker's diagnostic rather than us
    # silently picking the wrong one.
    local AGENT_NETWORK=""
    local i
    for ((i=0; i<${#EXTRA_RUN_ARGS[@]}; i++)); do
        case "${EXTRA_RUN_ARGS[$i]}" in
            --network|--net)
                if [ $((i+1)) -ge "${#EXTRA_RUN_ARGS[@]}" ]; then
                    echo "Error: '${EXTRA_RUN_ARGS[$i]}' in agent-run-args is missing a value (in $(pwd)/Dockerfile.agent)" >&2
                    return 1
                fi
                AGENT_NETWORK="${EXTRA_RUN_ARGS[$((i+1))]}"
                break
                ;;
            --network=*|--net=*)
                AGENT_NETWORK="${EXTRA_RUN_ARGS[$i]#*=}"
                break
                ;;
        esac
    done

    local HOST_NET_ARGS=()
    local use_host_net=""
    case "${AGENT_USE_HOST_NETWORK:-}" in
        ""|0|false|FALSE|no|NO) ;;
        *) use_host_net=1 ;;
    esac
    if [ -n "$use_host_net" ]; then
        if ! grep -qi microsoft /proc/version 2>/dev/null; then
            echo "Note: AGENT_USE_HOST_NETWORK ignored — only honored on WSL hosts." >&2
            use_host_net=""
        elif [ -n "$AGENT_NETWORK" ]; then
            echo "Warning: AGENT_USE_HOST_NETWORK ignored — Dockerfile.agent already specifies --network via agent-run-args." >&2
            use_host_net=""
        else
            HOST_NET_ARGS=(--network host)
            if [ "${#PORT_ARGS[@]}" -gt 0 ]; then
                echo "Warning: AGENT_USE_HOST_NETWORK is on — EXPOSE port mappings (${PORT_ARGS[*]}) skipped. Services bind on the WSL distro's interfaces directly; ensure they listen on 127.0.0.1 to avoid LAN exposure." >&2
                PORT_ARGS=()
            fi
        fi
    fi

    local CLAUDE_HOME="/home/${AGENT_USER}"

    local AGENT_NAME
    if [ "$USE_BASE" = "0" ] && [ -f "$(pwd)/Dockerfile.agent" ]; then
        AGENT_NAME=$(grep -oE '^#[[:space:]]*agent-name:[[:space:]]*\S+' "$(pwd)/Dockerfile.agent" | head -n1 | sed -E 's/^#[[:space:]]*agent-name:[[:space:]]*//')
    else
        AGENT_NAME=$(_agent_sanitize_name "$(basename "$(pwd)")")
        [ -z "$AGENT_NAME" ] && AGENT_NAME="agent"
    fi

    if [ -f "$SECRETS_FILE" ]; then
        local TELEGRAM_BOT_TOKEN=$(jq -r '.TelegramBotToken // ""' "$SECRETS_FILE")
        local TELEGRAM_CHAT_ID=$(jq -r '.TelegramChatId // ""' "$SECRETS_FILE")
    else
        echo "File ${SECRETS_FILE} not found"
        return 1
    fi

    local AGENT_INSTANCE_ID _instance_uuid
    _instance_uuid=$(_agent_uuid) || return 1
    AGENT_INSTANCE_ID="${AGENT_NAME}-${_instance_uuid}"

    mkdir -p "$GLOBAL_CONFIG_DIR/.claude"

    touch "$GLOBAL_CONFIG_DIR/.claude.json"
    touch "$GLOBAL_CONFIG_DIR/.claude/settings.json"
    chmod 600 "$GLOBAL_CONFIG_DIR/.claude.json"
    chmod 600 "$GLOBAL_CONFIG_DIR/.claude/settings.json"
    _agent_ensure_statusline "$GLOBAL_CONFIG_DIR/.claude/settings.json"
    if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        _agent_ensure_telegram_hooks "$GLOBAL_CONFIG_DIR/.claude/settings.json"
    fi
    if [ ! -f "$GLOBAL_CONFIG_DIR/.claude/CLAUDE.md" ]; then
        cp "$TOOL_DIR/default-CLAUDE.md" "$GLOBAL_CONFIG_DIR/.claude/CLAUDE.md"
    fi

    # Pre-create the projects dir inside global .claude so Docker doesn't create it as root
    mkdir -p "$GLOBAL_CONFIG_DIR/.claude/projects/-workspace"

    local PROJECT_CLAUDE_DIR="$(pwd)/.claude"
    mkdir -p \
        "$PROJECT_CLAUDE_DIR/sessions" \
        "$PROJECT_CLAUDE_DIR/plans" \
        "$PROJECT_CLAUDE_DIR/todos" \
        "$PROJECT_CLAUDE_DIR/tasks" \
        "$PROJECT_CLAUDE_DIR/shell-snapshots" \
        "$PROJECT_CLAUDE_DIR/session-env" \
        "$PROJECT_CLAUDE_DIR/file-history" \
        "$PROJECT_CLAUDE_DIR/paste-cache"

    if [ ! -f "$PROJECT_CLAUDE_DIR/.gitignore" ]; then
        echo '*' > "$PROJECT_CLAUDE_DIR/.gitignore"
    fi

    local WSLG_ARGS=()
    if [ -d /mnt/wslg ]; then
        WSLG_ARGS+=(
            -v /mnt/wslg:/mnt/wslg
            -v /mnt/wslg/.X11-unix:/tmp/.X11-unix
            -v "${TOOL_DIR}/wl-paste-shim:/usr/local/bin/wl-paste:ro"
            -e DISPLAY
            -e WAYLAND_DISPLAY
            -e XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir
        )
    fi

    _agent_record_project "$TOOL_DIR"

    echo "--- Agent instance: $AGENT_INSTANCE_ID ---"

    (
        trap '_provider_release "$TOOL_DIR" "$AGENT_INSTANCE_ID"' EXIT

        if ! _provider_ensure "$TOOL_DIR" "$use_host_net" "$AGENT_INSTANCE_ID" "$AGENT_NETWORK"; then
            echo "Error: failed to start provider '${AGENT_PROVIDER}'; aborting." >&2
            exit 1
        fi

        local LABEL_ARGS=()
        local _line
        while IFS= read -r _line; do
            [ -n "$_line" ] && LABEL_ARGS+=("$_line")
        done < <(_provider_label_args "$AGENT_INSTANCE_ID")

        echo "--- Launching Claude (Image: $IMAGE, Config: $GLOBAL_CONFIG_DIR) ---"

        docker run --rm -it \
            "${USER_ARGS[@]}" \
            -v "${GLOBAL_CONFIG_DIR}/.claude.json:${CLAUDE_HOME}/.claude.json" \
            -v "${GLOBAL_CONFIG_DIR}/.claude:${CLAUDE_HOME}/.claude" \
            -v "$(pwd):/workspace" \
            -v "$(pwd)/.claude/sessions:${CLAUDE_HOME}/.claude/projects/-workspace" \
            -v "$(pwd)/.claude/plans:${CLAUDE_HOME}/.claude/plans" \
            -v "$(pwd)/.claude/todos:${CLAUDE_HOME}/.claude/todos" \
            -v "$(pwd)/.claude/tasks:${CLAUDE_HOME}/.claude/tasks" \
            -v "$(pwd)/.claude/shell-snapshots:${CLAUDE_HOME}/.claude/shell-snapshots" \
            -v "$(pwd)/.claude/session-env:${CLAUDE_HOME}/.claude/session-env" \
            -v "$(pwd)/.claude/file-history:${CLAUDE_HOME}/.claude/file-history" \
            -v "$(pwd)/.claude/paste-cache:${CLAUDE_HOME}/.claude/paste-cache" \
            -v "${TOOL_DIR}/Dockerfile:${AGENT_WRAP_MOUNT}/Dockerfile:ro" \
            -v "${TOOL_DIR}/agent-wrap.bashrc:${AGENT_WRAP_MOUNT}/agent-wrap.bashrc:ro" \
            -v "${TOOL_DIR}/validate-dockerfile-agent:${AGENT_WRAP_MOUNT}/validate-dockerfile-agent:ro" \
            -v "${TOOL_DIR}/statusline.py:${AGENT_WRAP_MOUNT}/statusline.py:ro" \
            -v "${TOOL_DIR}/telegram-notify.sh:${AGENT_WRAP_MOUNT}/telegram-notify.sh:ro" \
            -v "${TOOL_DIR}/md_to_html.js:${AGENT_WRAP_MOUNT}/md_to_html.js:ro" \
            -e DISABLE_AUTOUPDATER=1 \
            -e TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN}" \
            -e TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID}" \
            -e AGENT_NAME="${AGENT_NAME}" \
            -e AGENT_INSTANCE_ID="${AGENT_INSTANCE_ID}" \
            -e TERM="${TERM:-xterm-256color}" \
            -e COLORTERM="${COLORTERM:-truecolor}" \
            -e HOME="${CLAUDE_HOME}" \
            "${LABEL_ARGS[@]}" \
            "${PROVIDER_EXTRA_RUN_ARGS[@]}" \
            "${PORT_ARGS[@]}" \
            "${WSLG_ARGS[@]}" \
            "${HOST_NET_ARGS[@]}" \
            "${EXTRA_RUN_ARGS[@]}" \
            "$IMAGE" "${CLAUDE_ARGS[@]}"
    )
}
