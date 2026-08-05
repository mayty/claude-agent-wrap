# This file has been created with the assistance of an AI tool.
"""Constants for the inspect CLI command."""

#: Spinner label shown while the report is collected.
INSPECT_LABEL = "inspect"

#: Placeholder for a value that could not be determined.
UNKNOWN = "?"

#: Placeholder for a value that is legitimately absent.
NONE_CELL = "—"

#: Columns of the sidecar table. The image follows the role because it qualifies it:
#: the role says what the container is for, the image says which build is serving it.
SIDECAR_HEADERS = ("CONTAINER", "ROLE", "IMAGE", "STATUS", "HEALTH", "UPTIME", "PORT", "AGENTS")
SIDECAR_ALIGNS = ("<", "<", "<", "<", "<", ">", ">", ">")

#: Columns of the agent table. Image and directory lead because they are what identifies
#: an agent at a glance; its instance id is an opaque token and its sidecar container
#: names are already listed in the sidecar table, so the provider stands in for them.
AGENT_HEADERS = ("IMAGE", "CWD", "PROVIDER", "STATUS", "UPTIME")
AGENT_ALIGNS = ("<", "<", "<", "<", ">")

#: Columns of the details table — everything that is not a container, in three groups
#: (logs, secrets, wrapper/host) separated by dividers.
DETAILS_TITLE = "Details:"
DETAILS_HEADERS = ("ITEM", "STATE")
DETAILS_ALIGNS = ("<", "<")

#: Long-form help printed for `agent inspect -h`.
USAGE_TEXT = (
    "Usage: agent inspect [--json]\n\n"
    "Reports what agent-wrap is currently doing on this host: the sidecar\n"
    "containers that are up (with their image, port, health, uptime, and how\n"
    "many agents are attached), the agent containers running against them (with\n"
    "their image, project directory, and provider), the logs viewer, the\n"
    "on-disk log footprint, per-provider secret readiness, the installed\n"
    "wrapper revision, and the host facts behind most launch surprises.\n\n"
    "Read-only: it starts nothing, stops nothing, and writes nothing. It also\n"
    "makes no network call, so the wrapper revision it reports is the local\n"
    "one — use `agent update` to check for a newer release.\n\n"
    "--json emits the same report as one JSON document instead of tables.\n\n"
    "Exits 1 when the Docker daemon cannot be reached; every section that does\n"
    "not depend on Docker is still reported."
)
