# This file has been created with the assistance of an AI tool.
#
# Provider plugin template — copy this directory to providers/<your-name>/
# and implement the three functions below. Select your provider with
# `AGENT_PROVIDER=<your-name>` before running `agent`.
#
# A provider is the layer between Claude Code (in the agent container) and
# the model API it talks to. The launcher (agent-wrap.bashrc) sources exactly
# one provider per session and calls it at three points in the agent
# lifecycle: just before `docker run` (ensure), after the agent exits
# (release), and to compute Docker labels for the agent container
# (label_args).
#
# All three functions are required. The output array PROVIDER_EXTRA_RUN_ARGS
# is the only side-channel from `_provider_ensure` back to the launcher —
# whatever flags the agent's `docker run` needs (env vars, --network,
# --add-host, etc.) must be packed into that array.
#
# These stubs intentionally fail loudly. A misconfigured `AGENT_PROVIDER=template`
# should not silently launch a half-working agent.
#
# See `providers/litellm-bedrock/provider.sh` for a working reference
# implementation.

# ---------- _provider_ensure ----------
#
# Called by the launcher right before it runs `docker run` for the agent.
#
# Args:
#   TOOL_DIR       Absolute path to the agent-wrap source directory. Use this
#                  for any wrapper-level state you need to persist across
#                  launches (refcount files, locks, caches) under
#                  $TOOL_DIR/.agent-launches/. Do NOT put provider-internal
#                  files outside that subdirectory.
#   USE_HOST_NET   "1" if the agent will run with `--network host`, empty
#                  otherwise. Useful if your provider runs its own container
#                  whose network namespace must match the agent's.
#   INSTANCE_ID    Per-launch unique ID (e.g. "myproj-<uuid>"). Use it to
#                  refcount concurrent agents if your provider holds shared
#                  state.
#   AGENT_NETWORK  Name of a user-defined Docker network the agent will run
#                  on (empty if the agent uses the default network or
#                  --network host). If your provider runs a sidecar that
#                  must be reachable from the agent, attach it to this
#                  network.
#
# Output:
#   PROVIDER_EXTRA_RUN_ARGS — a bash array. The launcher splices its
#                             contents into the agent's `docker run`
#                             verbatim. Typical contents:
#                               -e VAR=value             (env vars Claude Code reads)
#                               --network <name>         (if you need to pin a network)
#                               --add-host host:ip       (for cross-netns reachability)
#
# Return:
#   0 on success. Non-zero aborts the launch (no fallback) — print a clear
#   error to stderr first.
_provider_ensure() {
    echo "provider/template: _provider_ensure not implemented" >&2
    echo "  Copy providers/template/ to providers/<your-name>/ and implement this function." >&2
    return 1
}

# ---------- _provider_release ----------
#
# Called by the launcher's EXIT trap after the agent exits.
#
# Args:
#   TOOL_DIR     Same as in _provider_ensure.
#   INSTANCE_ID  The same per-launch ID passed to _provider_ensure.
#
# Responsibilities:
#   - Remove this instance from any refcount your provider keeps.
#   - If no instances remain and your provider runs a long-lived sidecar,
#     stop it.
#
# This function is called from a trap on exit and must be idempotent. It
# must not fail the calling shell — return 0 on every path, swallow errors.
_provider_release() {
    # Stub: nothing to release. Real providers should clean up here.
    return 0
}

# ---------- _provider_label_args ----------
#
# Pure function. Prints `--label`/`--name` flags (one per line) for the
# agent's `docker run`. Centralized here so renaming a label only requires
# editing this provider, not the launcher.
#
# Args:
#   INSTANCE_ID  Per-launch ID; typically used as the container --name
#                suffix and the value of an `agent-wrap.instance-id` label.
#
# Output:
#   Printed to stdout — one flag or value per line. The launcher reads them
#   into an array and splices them into `docker run`. Example output:
#
#     --label
#     agent-wrap.role=claude-agent
#     --label
#     agent-wrap.instance-id=myproj-abc123
#     --name
#     claude-agent-myproj-abc123
_provider_label_args() {
    echo "provider/template: _provider_label_args not implemented" >&2
    return 1
}
