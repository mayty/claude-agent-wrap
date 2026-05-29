<!-- This file has been created with the assistance of an AI tool. -->

# Provider plugin template

A **provider** is the layer between Claude Code (running in the agent container) and the model API it ultimately talks to. `agent-wrap` sources exactly one provider per session — selected by the `AGENT_PROVIDER` environment variable — and calls into it at well-defined points in the agent lifecycle.

This directory is a starting point for new providers. To add one:

1. Copy this directory:
   ```sh
   cp -r providers/template providers/my-provider
   ```
2. Edit `providers/my-provider/provider.sh` and implement the three required functions (see below).
3. Add any auxiliary files your provider needs (config, scripts, etc.) inside the same directory.
4. Select it:
   ```sh
   AGENT_PROVIDER=my-provider source agent-wrap.bashrc
   agent
   ```

If `AGENT_PROVIDER` is unset, the launcher defaults to `litellm-bedrock`. If it points at a directory with no `provider.sh`, the launcher fails fast and lists available providers — auto-discovered by globbing `providers/*/provider.sh`, so just dropping a new directory in is enough.

## The contract

The launcher relies on **three functions** and **one output global**. Names are fixed; renaming any of them breaks `agent-wrap.bashrc`.

### `_provider_ensure TOOL_DIR USE_HOST_NET INSTANCE_ID AGENT_NETWORK`

Called right before `docker run` for the agent. Sets up whatever your provider needs (start a sidecar, mint credentials, register state) and produces the run-args the agent's `docker run` should include.

| Arg | Meaning |
| --- | --- |
| `TOOL_DIR` | Absolute path to the agent-wrap source directory. Use `$TOOL_DIR/.agent-launches/` for any persistent state (locks, refcounts, caches). |
| `USE_HOST_NET` | `"1"` if the agent will run with `--network host`, empty otherwise. |
| `INSTANCE_ID` | Per-launch unique ID. Use it to refcount concurrent agents if you hold shared state. |
| `AGENT_NETWORK` | Name of a user-defined Docker network the agent will run on (empty for default, `host`, or `none`). Attach your sidecar here if you run one and it must be reachable from the agent. |

**Output**: populate the bash array `PROVIDER_EXTRA_RUN_ARGS` with flags the launcher should splice into the agent's `docker run`. Typical contents:

```bash
PROVIDER_EXTRA_RUN_ARGS=(
    -e ANTHROPIC_API_KEY="$key"
    -e ANTHROPIC_BASE_URL="$url"
    --network "$some_net"
    --add-host my-sidecar:127.0.0.1
)
```

**Return**: `0` on success; non-zero aborts the launch. There is **no fallback** — print a clear error to stderr before returning non-zero.

### `_provider_release TOOL_DIR INSTANCE_ID`

Called from the launcher's `EXIT` trap after the agent exits. Decrement your refcount; if no instances remain and you run a long-lived sidecar, stop it.

**Must be idempotent**, **must not fail the calling shell** (return `0` on every path, swallow errors).

### `_provider_label_args INSTANCE_ID`

Pure function. Prints `--label` / `--name` flags (one token per line) for the agent's `docker run`. Centralized here so renaming a label only edits the provider, not the launcher.

Example output:

```
--label
agent-wrap.role=claude-agent
--label
agent-wrap.instance-id=myproj-abc123
--name
claude-agent-myproj-abc123
```

The launcher reads the lines into an array and splices them into `docker run`.

## State and file layout

Anything inside `providers/<your-name>/` is fair game — store config files, helper scripts, etc. next to `provider.sh`. Resolve their paths from `${BASH_SOURCE[0]}`, **not** `$TOOL_DIR`:

```bash
my_provider_config() {
    local provider_dir
    provider_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    printf '%s/config.yaml\n' "$provider_dir"
}
```

This keeps each provider self-contained. `$TOOL_DIR` is for *wrapper*-level state (refcounts, locks under `.agent-launches/`) — not provider-internal layout.

## Reference implementation

See [`providers/litellm-bedrock/provider.sh`](../litellm-bedrock/provider.sh). It runs a shared LiteLLM container fronting AWS Bedrock and demonstrates:

- Lazy sidecar startup under `flock` with a Docker healthcheck wait.
- Refcount-driven shutdown (last agent out turns the sidecar off).
- Cross-netns reachability via `--add-host` and `host-gateway`.
- Master-key recovery from `docker inspect` (no on-disk persistence).

Most non-trivial providers will follow a similar shape.
