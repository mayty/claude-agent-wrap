# This file has been edited with the assistance of an AI tool.
_agent_resolve_image() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"

    if [ -f "$(pwd)/Dockerfile.agent" ]; then
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

create_custom_agent() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local SRC="$TOOL_DIR/Dockerfile"
    local DST="$(pwd)/Dockerfile.agent"

    if [ -e "$DST" ]; then
        echo "Error: $DST already exists" >&2
        return 1
    fi
    local NAME=$(basename "$(pwd)" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_.-' '-' | sed -E 's/-+$//; s/^-+//')
    if [ -z "$NAME" ]; then
        echo "Error: could not derive agent-name from directory '$(pwd)'" >&2
        return 1
    fi
    { echo "# agent-name: $NAME"; echo; cat "$SRC"; } > "$DST"
    echo "Created $DST with agent-name '$NAME'"
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
    local RESOLVED
    RESOLVED=$(_agent_resolve_image) || return 1
    local IMAGE DOCKERFILE CONTEXT
    IFS=$'\t' read -r IMAGE DOCKERFILE CONTEXT <<< "$RESOLVED"

    echo "--- Building $IMAGE from $DOCKERFILE ---"
    docker build --no-cache \
        --build-arg HOST_UID="$(id -u)" \
        --build-arg HOST_GID="$(id -g)" \
        -f "$DOCKERFILE" -t "$IMAGE" "$CONTEXT"
    docker images --filter "reference=$IMAGE"
}

agent() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"
    local GLOBAL_CONFIG_DIR="$TOOL_DIR/.claude_config"
    local SECRETS_FILE="${HOME}/claude_keys.json"
    local AGENT_USER="ubuntu"

    local RESOLVED
    RESOLVED=$(_agent_resolve_image) || return 1
    local IMAGE DOCKERFILE CONTEXT
    IFS=$'\t' read -r IMAGE DOCKERFILE CONTEXT <<< "$RESOLVED"

    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "Error: Image '$IMAGE' not found. Run 'rebuild_agent' in this directory to build it." >&2
        return 1
    fi

    local USER_ARGS=()
    if ! docker info 2>/dev/null | grep -qi 'rootless'; then
        USER_ARGS=(--user "$(id -u):$(id -g)")
    fi

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

    local CLAUDE_HOME="/home/${AGENT_USER}"

    if [ -f "$SECRETS_FILE" ]; then
        local CLAUDE_KEY=$(jq -r '.ServiceSpecificCredential.ServiceCredentialSecret' "$SECRETS_FILE")
    else
        echo "File ${SECRETS_FILE} not found"
        return 1
    fi

    mkdir -p "$GLOBAL_CONFIG_DIR/.claude"

    touch "$GLOBAL_CONFIG_DIR/.claude.json"
    touch "$GLOBAL_CONFIG_DIR/.claude/settings.json"
    chmod 600 "$GLOBAL_CONFIG_DIR/.claude.json"
    chmod 600 "$GLOBAL_CONFIG_DIR/.claude/settings.json"
    if [ ! -f "$GLOBAL_CONFIG_DIR/.claude/CLAUDE.md" ]; then
        cp "$TOOL_DIR/default-CLAUDE.md" "$GLOBAL_CONFIG_DIR/.claude/CLAUDE.md"
    fi

    # Pre-create the projects dir inside global .claude so Docker doesn't create it as root
    mkdir -p "$GLOBAL_CONFIG_DIR/.claude/projects/-workspace"

    local PROJECT_CLAUDE_DIR="$(pwd)/.claude"
    mkdir -p "$PROJECT_CLAUDE_DIR/sessions"

    if [ ! -f "$PROJECT_CLAUDE_DIR/.gitignore" ]; then
        echo '*' > "$PROJECT_CLAUDE_DIR/.gitignore"
    fi

    echo "--- Launching Claude (Image: $IMAGE, Config: $GLOBAL_CONFIG_DIR) ---"

    docker run --rm -it \
        "${USER_ARGS[@]}" \
        -v "${GLOBAL_CONFIG_DIR}/.claude.json:${CLAUDE_HOME}/.claude.json" \
        -v "${GLOBAL_CONFIG_DIR}/.claude:${CLAUDE_HOME}/.claude" \
        -v "$(pwd):/workspace" \
        -v "$(pwd)/.claude/sessions:${CLAUDE_HOME}/.claude/projects/-workspace" \
        -v "${TOOL_DIR}/Dockerfile:/opt/agent-wrap/Dockerfile:ro" \
        -v "${TOOL_DIR}/agent-wrap.bashrc:/opt/agent-wrap/agent-wrap.bashrc:ro" \
        -e AWS_BEARER_TOKEN_BEDROCK="${CLAUDE_KEY}" \
        -e HOME="${CLAUDE_HOME}" \
        "${PORT_ARGS[@]}" \
        "${EXTRA_RUN_ARGS[@]}" \
        "$IMAGE" "$@"
}
