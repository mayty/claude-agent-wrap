# This file has been created with the assistance of an AI tool.
"""Constants for the status domain subpackage."""

#: Env var overriding the stats day boundary. Its presence is reported so a user can
#: see why `agent stats` buckets days the way it does.
DAY_START_ENV = "AGENT_DAY_START_UTC"

#: Env var requesting the WSL host-network workaround. Honored only on WSL, so it is
#: reported as requested-vs-effective rather than as a single flag.
HOST_NETWORK_ENV = "AGENT_USE_HOST_NETWORK"

#: Message reported when the Docker daemon does not answer.
DOCKER_UNREACHABLE = (
    "Docker is not reachable — container state is unavailable. "
    "Is the daemon running, and is this user in the docker group?"
)
