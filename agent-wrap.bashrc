_agent_resolve_image() {
    local TOOL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")" && pwd)"

    if [ -f "$(pwd)/Dockerfile.agent" ]; then
        local DOCKERFILE="$(pwd)/Dockerfile.agent"
        local NAME=$(grep -oE '^#[[:space:]]*agent-name:[[:space:]]*\S+' "$DOCKERFILE" | head -n1 | sed -E 's/^#[[:space:]]*agent-name:[[:space:]]*//')
        if [ -z "$NAME" ]; then
            echo "Error: Dockerfile.agent must contain '# agent-name: <name>' comment" >&2
            return 1
        fi
        if ! [[ "$NAME" =~ ^[a-zA-Z0-9_.-]+$ ]]; then
            echo "Error: agent-name '$NAME' must match [a-zA-Z0-9_.-]+" >&2
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
    local NAME=$(basename "$(pwd)" | tr -c 'a-zA-Z0-9_.-' '-' | sed -E 's/-+$//; s/^-+//')
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
    git -C "$TOOL_DIR" pull --ff-only origin "$BRANCH" || return 1
    AFTER=$(git -C "$TOOL_DIR" rev-parse HEAD) || return 1
    local y=$'\033[1;33m' r=$'\033[0m'
    echo ""
    if [ "$BEFORE" = "$AFTER" ]; then
        echo "${y}Note:${r} already up to date; no action needed."
    else
        echo "${y}Note:${r} restart your shell (or re-source agent-wrap.bashrc) to pick up script changes."
        echo "${y}Note:${r} run 'rebuild_agent' to rebuild the Docker image with the updated files."
    fi
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
    local CLAUDE_HOME="/tmp/claude-home"

    local RESOLVED
    RESOLVED=$(_agent_resolve_image) || return 1
    local IMAGE DOCKERFILE CONTEXT
    IFS=$'\t' read -r IMAGE DOCKERFILE CONTEXT <<< "$RESOLVED"

    if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
        echo "Error: Image '$IMAGE' not found. Run 'rebuild_agent' in this directory to build it." >&2
        return 1
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

        while IFS= read -r line; do
            # shellcheck disable=SC2206
            EXTRA_RUN_ARGS+=(${line})
        done < <(grep -E '^#[[:space:]]*agent-run-args:[[:space:]]*.*' "$DOCKERFILE" \
                 | sed -E 's/^#[[:space:]]*agent-run-args:[[:space:]]*//')
    fi

    if [ -f "$SECRETS_FILE" ]; then
        local CLAUDE_KEY=$(jq -r '.ServiceSpecificCredential.ServiceCredentialSecret' "$SECRETS_FILE")
    else
        echo "File ${SECRETS_FILE} not found"
        return 1
    fi

    mkdir -p "$GLOBAL_CONFIG_DIR"
    touch "$GLOBAL_CONFIG_DIR/claude.json"
    touch "$GLOBAL_CONFIG_DIR/settings.json"
    chmod 600 "$GLOBAL_CONFIG_DIR/claude.json"
    chmod 600 "$GLOBAL_CONFIG_DIR/settings.json"

    local PROJECT_CLAUDE_DIR="$(pwd)/.claude"
    mkdir -p "$PROJECT_CLAUDE_DIR/sessions"

    if [ ! -f "$PROJECT_CLAUDE_DIR/.gitignore" ]; then
        echo '*' > "$PROJECT_CLAUDE_DIR/.gitignore"
    fi

    echo "--- Launching Claude (Image: $IMAGE, Config: $GLOBAL_CONFIG_DIR) ---"

    docker run --rm -it \
        --user "$(id -u):$(id -g)" \
        -v "${GLOBAL_CONFIG_DIR}/claude.json:${CLAUDE_HOME}/.claude.json" \
        -v "${GLOBAL_CONFIG_DIR}/settings.json:${CLAUDE_HOME}/.claude/settings.json" \
        -v "$(pwd):/workspace" \
        -v "$(pwd)/.claude/sessions:${CLAUDE_HOME}/.claude/projects/-workspace" \
        -e AWS_BEARER_TOKEN_BEDROCK="${CLAUDE_KEY}" \
        -e HOME=${CLAUDE_HOME} \
        "${PORT_ARGS[@]}" \
        "${EXTRA_RUN_ARGS[@]}" \
        "$IMAGE" "$@"
}
