# This file has been created with the assistance of an AI tool.
"""Constants for the status domain subpackage."""

#: Env var overriding the stats day boundary. Its presence is reported so a user can
#: see why `agent stats` buckets days the way it does.
DAY_START_ENV = "AGENT_DAY_START_UTC"

#: Env var naming an IANA zone used as the day-boundary fallback when DAY_START_ENV is
#: unset. Its presence is reported for the same reason as DAY_START_ENV above.
TIMEZONE_ENV = "AGENT_TIMEZONE"

#: Env var requesting the WSL host-network workaround. Honored only on WSL, so it is
#: reported as requested-vs-effective rather than as a single flag.
HOST_NETWORK_ENV = "AGENT_USE_HOST_NETWORK"

#: Message reported when the Docker daemon does not answer.
DOCKER_UNREACHABLE = (
    "Docker is not reachable — container state is unavailable. "
    "Is the daemon running, and is this user in the docker group?"
)

#: Threads the report's Docker probes fan out over. Peak in-flight is seven: the two
#: container listings, the network check and the logs-size walk are all still running
#: when the up-to-three version probes are submitted. Sizing this below the peak does
#: not fail — it silently re-serialises the version probes behind the listings, which is
#: the whole cost this pool exists to remove, so the number is stated with its reason.
PROBE_WORKERS = 8

#: Thread-name prefix for those probes, so a stuck one is identifiable in a stack dump.
PROBE_THREAD_PREFIX = "inspect-probe"
