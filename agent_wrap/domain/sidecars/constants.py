# This file has been edited with the assistance of an AI tool.
"""Constants for the sidecars domain."""

from agent_wrap.constants import BASE_IMAGE_NAME

#: Docker label name used to identify agent containers.
ROLE_LABEL = "agent-wrap.role"
#: Docker label value identifying agent containers.
ROLE_VALUE = BASE_IMAGE_NAME
#: Docker label carrying an agent's instance id — the flock registry's key.
INSTANCE_ID_LABEL = "agent-wrap.instance-id"

#: How many successive ports a cold start probes before giving up.
PORT_SCAN_LIMIT = 50

#: Container path the project directory is bind-mounted at, and so the mount entry
#: whose source recovers an agent's cwd.
WORKSPACE_MOUNT_DEST = "/workspace"

#: Role names reported for a discovered sidecar container.
LITELLM_ROLE = "litellm"
TELEGRAM_ROLE = "telegram"
UNKNOWN_ROLE = "unknown"

#: The only container env vars a report may read. Everything else in a sidecar's
#: environment is a live credential (LITELLM_MASTER_KEY, the upstream provider token,
#: TELEGRAM_BOT_TOKEN), so the parse allowlists rather than filters.
INSPECTABLE_ENV_KEYS = frozenset({"AGENT_WRAP_SIDECAR_PORT", "AGENT_WRAP_PROVIDER"})

#: Field separator for the batched `docker container inspect` template. Every
#: composite field is wrapped in {{json}}, which escapes tabs and newlines, so a tab
#: cannot appear inside a value and each container renders on exactly one line.
INSPECT_FIELD_SEP = "\t"

#: Batched-inspect template for sidecar containers. `.State.Health` is nil on a
#: container with no health check, and a nil field access fails the WHOLE invocation
#: rather than one row — hence the explicit guard.
SIDECAR_INSPECT_TEMPLATE = (
    "{{.Name}}\t"
    "{{.State.Status}}\t"
    "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}\t"
    "{{.State.StartedAt}}\t"
    "{{.State.ExitCode}}\t"
    "{{.Config.Image}}\t"
    "{{json .Config.Env}}\t"
    "{{json .NetworkSettings.Ports}}\t"
    "{{json .NetworkSettings.Networks}}"
)

#: Batched-inspect template for agent containers.
AGENT_INSPECT_TEMPLATE = (
    "{{.Name}}\t"
    "{{.State.Status}}\t"
    "{{.State.StartedAt}}\t"
    "{{.Config.Image}}\t"
    "{{json .Config.Labels}}\t"
    "{{json .Mounts}}"
)
