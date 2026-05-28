# This file has been created with the assistance of an AI tool.
#
# LiteLLM sidecar lifecycle for agent-wrap.
#
# Forks override this file to swap the proxy implementation, change the image
# pin, point at a hosted LiteLLM, etc. To keep upstream syncs clean, the public
# contract is intentionally narrow:
#
#   _litellm_sidecar_ensure   TOOL_DIR USE_HOST_NET INSTANCE_ID AGENT_NETWORK
#       Starts/reuses the shared sidecar; registers INSTANCE_ID in the
#       refcount; sets these output globals for the caller:
#         LITELLM_BASE_URL       — Claude Code's ANTHROPIC_BEDROCK_BASE_URL
#         LITELLM_BEARER_TOKEN   — Claude Code's AWS_BEARER_TOKEN_BEDROCK
#         LITELLM_EXTRA_RUN_ARGS — array; flags to splice into `docker run`
#       Returns non-zero on any failure (no silent fallback).
#
#       AGENT_NETWORK is the user-defined Docker network the agent will run on
#       (empty string for default). When non-empty and not "host"/"none", the
#       sidecar is attached to that network so the agent can resolve it by
#       container name without traversing the host FORWARD chain. When empty,
#       the launcher should add `--network agent-wrap-net` to the agent's
#       run args (the sidecar is always on that network).
#
#       The Bedrock key is read inline from
#       ${HOME}/claude_keys.json (.ServiceSpecificCredential.ServiceCredentialSecret).
#       Forks pointing at a hosted LiteLLM can ignore that file entirely —
#       just rewrite this function to skip the read.
#
#   _litellm_sidecar_release  TOOL_DIR INSTANCE_ID
#       Removes INSTANCE_ID from the refcount; stops the sidecar if no
#       instances remain. Idempotent and never fails the calling shell.
#
#   _litellm_sidecar_label_args  INSTANCE_ID
#       Pure function. Prints `--label`/`--name` flags for the agent's
#       `docker run`. Centralized so renaming the label only edits this file.
#
# Startup readiness uses Docker's built-in healthcheck (passed via
# --health-cmd to `docker run`); no host-side `curl` required.
#
# The proxy's master key lives only in the running sidecar's env. On launches
# that find the sidecar already running, it is recovered via `docker inspect`
# rather than persisted to disk — anyone who can read a file in the wrap-dir
# can already `docker exec` into the container, so disk persistence wasn't
# adding a security boundary.
#
# Renaming any of the above (or the LITELLM_* output globals) breaks
# agent-wrap.bashrc — change both files together if you must.

# ---------- Constants (most-likely-to-be-forked knobs) ----------

# Image pin: tag for readability + digest for reproducibility.
readonly _LITELLM_IMAGE="ghcr.io/berriai/litellm:v1.83.14-stable@sha256:c81eb79cd4333c6cfe374c0ec929110fd23f0ee5f7fd198855a6fbddc77b83ba"

readonly _LITELLM_CONTAINER="agent-wrap-litellm"
# Sidecar's primary user-defined network. Created on demand. Default-network
# agents are launched on this same network so they can reach the sidecar by
# container name without traversing the host's FORWARD chain (which a parallel
# WSL distro may have set to DROP). Agents on a project-supplied custom
# network instead get the sidecar attached to their network at launch time.
readonly _LITELLM_NETWORK="agent-wrap-net"
readonly _LITELLM_INTERNAL_PORT=4000
readonly _LITELLM_HEALTH_TIMEOUT_SEC=90
# Must exceed _LITELLM_HEALTH_TIMEOUT_SEC: parallel launches may queue behind a
# peer that is currently in the start-and-wait-for-health critical section.
readonly _LITELLM_LOCK_TIMEOUT_SEC=120

# Per-tool-dir state (paths derived from $TOOL_DIR; not constants).
__litellm_state_dir()    { printf '%s/.agent-launches\n' "$1"; }
__litellm_lock_file()    { printf '%s/.agent-launches/litellm.lock\n' "$1"; }
__litellm_refcount_file(){ printf '%s/.agent-launches/litellm.refcount\n' "$1"; }
__litellm_config_file()  { printf '%s/litellm/config.yaml\n' "$1"; }

# ---------- Public: ensure ----------

_litellm_sidecar_ensure() {
    local TOOL_DIR="$1" USE_HOST_NET="$2" INSTANCE_ID="$3" AGENT_NETWORK="${4:-}"
    if [ -z "$TOOL_DIR" ] || [ -z "$INSTANCE_ID" ]; then
        echo "litellm-sidecar: ensure() requires TOOL_DIR, USE_HOST_NET, INSTANCE_ID" >&2
        return 2
    fi
    if ! command -v docker >/dev/null 2>&1; then
        echo "litellm-sidecar: docker not found on host" >&2
        return 1
    fi
    if ! command -v flock >/dev/null 2>&1; then
        echo "litellm-sidecar: flock not found on host" >&2
        return 1
    fi
    if ! command -v jq >/dev/null 2>&1; then
        echo "litellm-sidecar: jq not found on host (used to parse claude_keys.json)" >&2
        return 1
    fi

    local SECRETS_FILE="${HOME}/claude_keys.json"
    if [ ! -f "$SECRETS_FILE" ]; then
        echo "litellm-sidecar: $SECRETS_FILE not found" >&2
        return 1
    fi
    if ! jq -e . "$SECRETS_FILE" >/dev/null 2>&1; then
        echo "litellm-sidecar: $SECRETS_FILE is not valid JSON" >&2
        return 1
    fi
    local BEDROCK_KEY
    BEDROCK_KEY=$(jq -r '.ServiceSpecificCredential.ServiceCredentialSecret // empty' "$SECRETS_FILE")
    if [ -z "$BEDROCK_KEY" ]; then
        echo "litellm-sidecar: .ServiceSpecificCredential.ServiceCredentialSecret missing or empty in $SECRETS_FILE" >&2
        return 1
    fi

    local STATE_DIR MASTER_KEY
    STATE_DIR="$(__litellm_state_dir "$TOOL_DIR")"
    mkdir -p "$STATE_DIR" || return 1

    # Acquire the lock for the duration of (start-if-needed + register +
    # cross-network attach). Attaching the sidecar to a project's custom
    # network must also be serialized — otherwise two parallel launches on
    # the same network race `docker network connect`.
    local LOCK_FILE
    LOCK_FILE="$(__litellm_lock_file "$TOOL_DIR")"
    exec 9>"$LOCK_FILE" || return 1
    if ! flock -w "$_LITELLM_LOCK_TIMEOUT_SEC" 9; then
        echo "litellm-sidecar: timed out waiting for lock $LOCK_FILE" >&2
        exec 9>&-
        return 1
    fi

    if ! __litellm_ensure_network; then
        flock -u 9; exec 9>&-; return 1
    fi

    # Migration safety net: an older sidecar started before this network
    # refactor won't be attached to agent-wrap-net. Reuse-as-is would leave
    # agents unable to resolve `agent-wrap-litellm`. Force a restart so the
    # new connectivity model takes effect.
    if __litellm_is_running && ! __litellm_is_on_network "$_LITELLM_NETWORK"; then
        echo "litellm-sidecar: existing sidecar predates agent-wrap-net; restarting" >&2
        docker stop "$_LITELLM_CONTAINER" >/dev/null 2>&1 || true
    fi

    if __litellm_is_running; then
        MASTER_KEY="$(__litellm_recover_master_key)" || {
            flock -u 9; exec 9>&-; return 1; }
    else
        MASTER_KEY="$(__litellm_generate_master_key)" || {
            flock -u 9; exec 9>&-; return 1; }
        if ! __litellm_start "$TOOL_DIR" "$BEDROCK_KEY" "$MASTER_KEY"; then
            flock -u 9; exec 9>&-; return 1
        fi
        if ! __litellm_health_poll; then
            echo "litellm-sidecar: health check failed; recent logs:" >&2
            docker logs --tail 50 "$_LITELLM_CONTAINER" >&2 2>&1 || true
            docker stop "$_LITELLM_CONTAINER" >/dev/null 2>&1 || true
            flock -u 9; exec 9>&-
            return 1
        fi
    fi

    # If the agent will run on a project-supplied network, attach the sidecar
    # to it so the agent can resolve `agent-wrap-litellm` by name without
    # crossing the host FORWARD chain. Skip for the magic networks "host" /
    # "none" (incompatible with `docker network connect`) and for our own
    # network (already attached at start time).
    if [ -n "$AGENT_NETWORK" ] \
       && [ "$AGENT_NETWORK" != "host" ] \
       && [ "$AGENT_NETWORK" != "none" ] \
       && [ "$AGENT_NETWORK" != "$_LITELLM_NETWORK" ]; then
        if ! __litellm_attach_to_network "$AGENT_NETWORK"; then
            flock -u 9; exec 9>&-; return 1
        fi
    fi

    __litellm_register_instance "$TOOL_DIR" "$INSTANCE_ID"

    flock -u 9
    exec 9>&-

    LITELLM_BEARER_TOKEN="$MASTER_KEY"
    LITELLM_BASE_URL="http://${_LITELLM_CONTAINER}:${_LITELLM_INTERNAL_PORT}/bedrock"
    if [ -n "$USE_HOST_NET" ]; then
        # Host-network agent: it sees the sidecar via the host's docker0/
        # agent-wrap-net bridge IPs but DNS-by-name doesn't work outside a
        # user-defined network. Resolve once via `docker inspect` and add a
        # /etc/hosts entry into the agent container so the same URL resolves.
        local SIDECAR_IP
        SIDECAR_IP=$(__litellm_sidecar_ip_on_network "$_LITELLM_NETWORK") || true
        if [ -z "$SIDECAR_IP" ]; then
            echo "litellm-sidecar: unable to resolve sidecar IP on $_LITELLM_NETWORK" >&2
            return 1
        fi
        LITELLM_EXTRA_RUN_ARGS=(--add-host "${_LITELLM_CONTAINER}:${SIDECAR_IP}")
    elif [ -z "$AGENT_NETWORK" ]; then
        # Default-bridge agent: put it on agent-wrap-net so DNS-by-name works.
        LITELLM_EXTRA_RUN_ARGS=(--network "$_LITELLM_NETWORK")
    else
        # Project-supplied network: sidecar was just attached above; agent
        # will use its existing --network from agent-run-args.
        LITELLM_EXTRA_RUN_ARGS=()
    fi
    return 0
}

# ---------- Public: release ----------

_litellm_sidecar_release() {
    local TOOL_DIR="$1" INSTANCE_ID="$2"
    [ -n "$TOOL_DIR" ] && [ -n "$INSTANCE_ID" ] || return 0
    command -v docker >/dev/null 2>&1 || return 0
    command -v flock  >/dev/null 2>&1 || return 0

    local LOCK_FILE
    LOCK_FILE="$(__litellm_lock_file "$TOOL_DIR")"
    [ -e "$LOCK_FILE" ] || return 0

    exec 9>"$LOCK_FILE" 2>/dev/null || return 0
    if ! flock -w "$_LITELLM_LOCK_TIMEOUT_SEC" 9; then
        exec 9>&-
        return 0
    fi

    __litellm_unregister_instance "$TOOL_DIR" "$INSTANCE_ID"
    __litellm_reconcile_refcount "$TOOL_DIR"

    if ! __litellm_has_active_instances "$TOOL_DIR"; then
        if __litellm_is_running; then
            docker stop "$_LITELLM_CONTAINER" >/dev/null 2>&1 || true
        fi
    fi

    flock -u 9
    exec 9>&-
    return 0
}

# ---------- Public: label_args ----------

_litellm_sidecar_label_args() {
    local INSTANCE_ID="$1"
    [ -n "$INSTANCE_ID" ] || return 0
    printf -- '--label\nagent-wrap.role=claude-agent\n--label\nagent-wrap.instance-id=%s\n--name\nclaude-agent-%s\n' \
        "$INSTANCE_ID" "$INSTANCE_ID"
}

# ---------- Internal helpers ----------

__litellm_generate_master_key() {
    local UUID
    UUID=$(cat /proc/sys/kernel/random/uuid) || return 1
    printf 'sk-aw-%s' "${UUID//-/}"
}

# Recover the master key from the running sidecar's env. Called only when
# __litellm_is_running has already returned true. If the env line is absent
# (which shouldn't happen — every start passes -e LITELLM_MASTER_KEY=...), we
# bail loudly rather than silently mint a new key, since that would 401 every
# in-flight agent already holding the existing key.
__litellm_recover_master_key() {
    local ENV_DUMP KEY
    ENV_DUMP=$(docker inspect "$_LITELLM_CONTAINER" \
        --format='{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null) || {
        echo "litellm-sidecar: docker inspect failed while recovering master key" >&2
        return 1
    }
    KEY=$(printf '%s\n' "$ENV_DUMP" | sed -n 's/^LITELLM_MASTER_KEY=//p' | head -n1)
    if [ -z "$KEY" ]; then
        echo "litellm-sidecar: LITELLM_MASTER_KEY not present in $_LITELLM_CONTAINER env; aborting" >&2
        return 1
    fi
    printf '%s' "$KEY"
}

__litellm_is_running() {
    local STATUS
    STATUS=$(docker container inspect -f '{{.State.Running}}' "$_LITELLM_CONTAINER" 2>/dev/null) || return 1
    [ "$STATUS" = "true" ]
}

__litellm_is_on_network() {
    local NETWORK="$1"
    local NETWORKS
    NETWORKS=$(docker inspect "$_LITELLM_CONTAINER" \
        --format '{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}' 2>/dev/null) || return 1
    printf '%s\n' "$NETWORKS" | grep -Fxq "$NETWORK"
}

__litellm_start() {
    local TOOL_DIR="$1" BEDROCK_KEY="$2" MASTER_KEY="$3"
    local CONFIG_FILE
    CONFIG_FILE="$(__litellm_config_file "$TOOL_DIR")"
    if [ ! -r "$CONFIG_FILE" ]; then
        echo "litellm-sidecar: config not found at $CONFIG_FILE" >&2
        return 1
    fi

    # Reap any stopped (Created/Exited) container under our name so `docker run`
    # doesn't fail with "name in use". --rm covers normal exits; this covers
    # crash-recovery on the next launch.
    if docker container inspect "$_LITELLM_CONTAINER" >/dev/null 2>&1; then
        docker rm -f "$_LITELLM_CONTAINER" >/dev/null 2>&1 || true
    fi

    # Healthcheck runs inside the container. The base image has python3 but
    # not curl/wget/nc, so urllib.request is the only stdlib option. Bound the
    # whole startup wait by retries*interval (~20s) so a wedged check can't
    # hang the launcher.
    local HEALTH_CMD
    HEALTH_CMD='python3 -c "import urllib.request; urllib.request.urlopen('"'"'http://127.0.0.1:'"${_LITELLM_INTERNAL_PORT}"'/health/liveliness'"'"', timeout=2).read()"'

    # No -p publish: agents reach the sidecar over agent-wrap-net (or a
    # network the sidecar is later attached to), not via host port forwarding.
    # Skipping the publish sidesteps the FORWARD=DROP scenario triggered by
    # parallel WSL distros' dockerds fighting over iptables-legacy rules.
    docker run -d --rm \
        --name "$_LITELLM_CONTAINER" \
        --network "$_LITELLM_NETWORK" \
        --health-cmd "$HEALTH_CMD" \
        --health-interval=30s \
        --health-retries=3 \
        --health-timeout=2s \
        --health-start-period=${_LITELLM_HEALTH_TIMEOUT_SEC}s \
        --health-start-interval=100ms \
        -e AWS_REGION_NAME=us-east-1 \
        -e AWS_BEARER_TOKEN_BEDROCK="$BEDROCK_KEY" \
        -e LITELLM_MASTER_KEY="$MASTER_KEY" \
        -v "$(__litellm_config_file "$TOOL_DIR"):/etc/litellm/config.yaml:ro" \
        "$_LITELLM_IMAGE" \
        --config /etc/litellm/config.yaml --port "$_LITELLM_INTERNAL_PORT" \
        >/dev/null
}

# Idempotently create the user-defined bridge the sidecar lives on. Driver
# bridge gives us container-name DNS resolution (which the default `bridge`
# does not), and a dedicated network keeps stray default-bridge containers
# off the proxy.
__litellm_ensure_network() {
    if docker network inspect "$_LITELLM_NETWORK" >/dev/null 2>&1; then
        return 0
    fi
    if ! docker network create "$_LITELLM_NETWORK" >/dev/null 2>&1; then
        echo "litellm-sidecar: failed to create docker network $_LITELLM_NETWORK" >&2
        return 1
    fi
}

# Connect the sidecar to NETWORK if it isn't already. Used to make the
# sidecar reachable from agents that run on a project-supplied network
# (`--network X` in agent-run-args).
__litellm_attach_to_network() {
    local NETWORK="$1"
    if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
        echo "litellm-sidecar: network '$NETWORK' (from agent-run-args) does not exist" >&2
        return 1
    fi
    local CONNECTED
    CONNECTED=$(docker inspect "$_LITELLM_CONTAINER" \
        --format '{{range $k, $_ := .NetworkSettings.Networks}}{{println $k}}{{end}}' 2>/dev/null) || return 1
    if printf '%s\n' "$CONNECTED" | grep -Fxq "$NETWORK"; then
        return 0
    fi
    if ! docker network connect "$NETWORK" "$_LITELLM_CONTAINER" >/dev/null 2>&1; then
        echo "litellm-sidecar: failed to attach $_LITELLM_CONTAINER to network '$NETWORK'" >&2
        return 1
    fi
}

# Print the sidecar's IP address on NETWORK. Used when the agent runs in the
# host network namespace and can't use docker DNS.
__litellm_sidecar_ip_on_network() {
    local NETWORK="$1"
    docker inspect "$_LITELLM_CONTAINER" \
        --format "{{with index .NetworkSettings.Networks \"$NETWORK\"}}{{.IPAddress}}{{end}}" 2>/dev/null
}

__litellm_health_poll() {
    local DEADLINE=$(( SECONDS + _LITELLM_HEALTH_TIMEOUT_SEC ))
    local STATUS LAST=""
    local TTY=0
    [ -t 2 ] && TTY=1
    while [ "$SECONDS" -lt "$DEADLINE" ]; do
        STATUS=$(docker inspect "$_LITELLM_CONTAINER" \
            --format='{{.State.Health.Status}}' 2>/dev/null) || { __litellm_health_progress_end "$TTY" "fail"; return 1; }
        if [ -n "$STATUS" ] && [ "$STATUS" != "$LAST" ]; then
            __litellm_health_progress "$TTY" "$STATUS"
            LAST="$STATUS"
        fi
        case "$STATUS" in
            healthy)   __litellm_health_progress_end "$TTY" "ok";   return 0 ;;
            unhealthy) __litellm_health_progress_end "$TTY" "fail"; return 1 ;;
        esac
        if ! __litellm_is_running; then
            __litellm_health_progress_end "$TTY" "fail"
            return 1
        fi
        sleep 0.5
    done
    __litellm_health_progress_end "$TTY" "fail"
    return 1
}

__litellm_health_progress() {
    local TTY="$1" STATUS="$2"
    if [ "$TTY" = "1" ]; then
        printf '\r\033[2Klitellm-sidecar: waiting for healthy [%s]' "$STATUS" >&2
    else
        printf 'litellm-sidecar: %s\n' "$STATUS" >&2
    fi
}

__litellm_health_progress_end() {
    local TTY="$1" RESULT="$2"
    if [ "$TTY" = "1" ]; then
        case "$RESULT" in
            ok)   printf '\r\033[2Klitellm-sidecar: ready\n' >&2 ;;
            fail) printf '\n' >&2 ;;
        esac
    fi
}

__litellm_register_instance() {
    local TOOL_DIR="$1" INSTANCE_ID="$2"
    local F
    F="$(__litellm_refcount_file "$TOOL_DIR")"
    touch "$F"
    grep -Fxq "$INSTANCE_ID" "$F" 2>/dev/null || printf '%s\n' "$INSTANCE_ID" >> "$F"
}

__litellm_unregister_instance() {
    local TOOL_DIR="$1" INSTANCE_ID="$2"
    local F TMP
    F="$(__litellm_refcount_file "$TOOL_DIR")"
    [ -f "$F" ] || return 0
    TMP="${F}.tmp.$$"
    grep -Fxv "$INSTANCE_ID" "$F" > "$TMP" 2>/dev/null || true
    mv "$TMP" "$F" 2>/dev/null || rm -f "$TMP"
}

__litellm_has_active_instances() {
    local TOOL_DIR="$1"
    local F
    F="$(__litellm_refcount_file "$TOOL_DIR")"
    [ -s "$F" ]
}

# Drop refcount entries whose claude-agent container no longer exists. Recovers
# from crashed launches that never ran their EXIT trap.
__litellm_reconcile_refcount() {
    local TOOL_DIR="$1"
    local F
    F="$(__litellm_refcount_file "$TOOL_DIR")"
    [ -s "$F" ] || return 0

    local LIVE
    LIVE=$(docker ps --filter "label=agent-wrap.role=claude-agent" \
                     --format '{{.Label "agent-wrap.instance-id"}}' 2>/dev/null) || return 0

    local TMP="${F}.tmp.$$"
    : > "$TMP"
    while IFS= read -r id; do
        [ -z "$id" ] && continue
        if printf '%s\n' "$LIVE" | grep -Fxq "$id"; then
            printf '%s\n' "$id" >> "$TMP"
        fi
    done < "$F"
    mv "$TMP" "$F" 2>/dev/null || rm -f "$TMP"
}
