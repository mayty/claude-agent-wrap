# This file has been edited with the assistance of an AI tool.
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

#: Label of the details row describing the image the cwd's Dockerfile declares. Named
#: "project image" rather than "custom image" to match the `.claude-agent-wrap/Dockerfile`
#: vocabulary the rest of the wrapper and its docs use.
PROJECT_IMAGE_LABEL = "project image"

#: Suffix marking a project image still built from the deprecated Dockerfile location.
LEGACY_DOCKERFILE_NOTE = " (from deprecated Dockerfile.agent)"

#: Columns of the stale-images table. The project leads because it is what the reader acts
#: on -- they `cd` there, or accept the rebuild the next launch will do; the image is the
#: thing that would be rebuilt, and repeats when two projects share an `# agent-name:`.
STALE_IMAGES_HEADERS = ("PROJECT", "IMAGE", "REASON")
STALE_IMAGES_ALIGNS = ("<", "<", "<")

#: Columns of that table which may be cut short when the console is narrow: the image name
#: and the build reason, both prose. PROJECT is absent deliberately -- a path is what the
#: reader acts on, and half of one identifies nothing, so the tree is chopped instead.
STALE_IMAGES_ELIDE = (1, 2)

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

#: Columns of the details table — everything that is not a container, in three groups
#: (logs, secrets, wrapper/host) separated by dividers.
DETAILS_TITLE = "Details:"
DETAILS_HEADERS = ("ITEM", "STATE")
DETAILS_ALIGNS = ("<", "<")

#: Long-form help printed for `agent inspect -h`.
USAGE_TEXT = (
    "Usage: agent inspect [--json] [--lite]\n\n"
    "Reports what agent-wrap is currently doing on this host: the sidecar\n"
    "containers that are up (with their image, port, health, uptime, and how\n"
    "many agents are attached), the agent containers running against them (with\n"
    "their image, project directory, and provider), the logs viewer, the\n"
    "on-disk log footprint, per-provider secret readiness, the installed\n"
    "wrapper revision, the Claude Code version in the base image and in this\n"
    "project's own image, and the host facts behind most launch surprises.\n\n"
    "It closes with every registered project whose own image is already stale,\n"
    "so the rebuilds coming across the whole fleet are stated before they are\n"
    "paid for -- or one green line when there are none. A project that\n"
    "declares no Dockerfile is not listed (the base image row above covers\n"
    "it), nor is one whose image was never built on this host.\n\n"
    "Read-only: it starts no agent, stops nothing, and writes nothing. It does\n"
    "start a throwaway container per image to read the Claude Code version\n"
    "installed there, and one of those queries the npm registry to report\n"
    "whether a newer version exists. The wrapper revision is always read\n"
    "locally — use `agent update` to check for a newer release.\n\n"
    "--lite skips the three slowest steps: the npm-registry check, the walk\n"
    "over the shared logs tree, and the stale-image sweep over the project\n"
    "registry. Everything else is reported as usual, including both installed\n"
    "Claude Code versions. Use it from a project startup script, which runs\n"
    "while holding the host-global startup lock.\n\n"
    "--json emits the same report as one JSON document instead of tables.\n\n"
    "Exits 1 when the Docker daemon cannot be reached; every section that does\n"
    "not depend on Docker is still reported."
)
