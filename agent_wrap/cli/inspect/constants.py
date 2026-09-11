# This file has been edited with the assistance of an AI tool.
"""Constants for the inspect CLI command."""

from agent_wrap.domain.display.models import TableSpec

#: Spinner label shown while the report is collected.
INSPECT_LABEL = "inspect"

#: Placeholder for a value that could not be determined.
UNKNOWN = "?"

#: Placeholder for a value that is legitimately absent.
NONE_CELL = "—"

#: The sidecar table. The image follows the role because it qualifies it: the role says
#: what the container is for, the image says which build is serving it.
SIDECAR_TABLE = TableSpec(
    headers=("CONTAINER", "ROLE", "IMAGE", "STATUS", "HEALTH", "UPTIME", "PORT", "AGENTS"),
    aligns=("<", "<", "<", "<", "<", ">", ">", ">"),
    leading=1,
)

#: The agent table. Image and directory lead because they are what identifies an agent at
#: a glance; its instance id is an opaque token and its sidecar container names are
#: already listed in the sidecar table, so the provider stands in for them.
AGENT_TABLE = TableSpec(
    headers=("IMAGE", "CWD", "PROVIDER", "STATUS", "UPTIME"),
    aligns=("<", "<", "<", "<", ">"),
    leading=1,
)

#: Label of the details row describing the image the cwd's Dockerfile declares. Named
#: "project image" rather than "custom image" to match the `.claude-agent-wrap/Dockerfile`
#: vocabulary the rest of the wrapper and its docs use.
PROJECT_IMAGE_LABEL = "project image"

#: Suffix marking a project image still built from the deprecated Dockerfile location.
LEGACY_DOCKERFILE_NOTE = " (from deprecated Dockerfile.agent)"

#: The stale-images table. The project leads because it is what the reader acts on -- they
#: `cd` there, or accept the rebuild the next launch will do; the image is the thing that
#: would be rebuilt, and repeats when two projects share an `# agent-name:`.
#:
#: Its elidable columns are the image name and the build reason, both prose. PROJECT is
#: absent deliberately -- a path is what the reader acts on, and half of one identifies
#: nothing, so the tree is chopped instead.
STALE_IMAGES_TABLE = TableSpec(
    headers=("PROJECT", "IMAGE", "REASON"),
    aligns=("<", "<", "<"),
    leading=1,
    elide=(1, 2),
)

#: Replaces the stale-images table when the sweep found nothing. Printed in green through
#: `DisplayService.success`, being the one section whose empty state is good news.
NO_STALE_IMAGES = "No stale images: every registered project's image is up to date."

#: Stands in for the logs footprint in lite mode. Says "not measured" rather than showing
#: nothing, because a blank cell next to a project count reads as zero bytes.
NOT_MEASURED = "not measured (--lite)"

#: Closing line of a lite report, naming exactly what was traded away for the speed.
LITE_NOTE = (
    "  --lite: skipped the npm-registry version check, the logs-size walk, and the "
    "stale-image sweep"
)

#: The details table — everything that is not a container, in three groups
#: (logs, secrets, wrapper/host) separated by dividers.
DETAILS_TITLE = "Details:"
DETAILS_TABLE = TableSpec(headers=("ITEM", "STATE"), aligns=("<", "<"), leading=1)
