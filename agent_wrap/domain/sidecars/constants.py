# This file has been created with the assistance of an AI tool.
"""Constants for the sidecars domain."""

from agent_wrap.constants import BASE_IMAGE_NAME

#: Docker label name used to identify agent containers.
ROLE_LABEL = "agent-wrap.role"
#: Docker label value identifying agent containers.
ROLE_VALUE = BASE_IMAGE_NAME

#: How many successive ports a cold start probes before giving up.
PORT_SCAN_LIMIT = 50
#: Container env var carrying the port a sidecar resolved at cold start. The running
#: container is the single source of truth: later launches recover it from here rather
#: than re-scanning (which would pick a different port and break connectivity).
SIDECAR_PORT_ENV = "AGENT_WRAP_SIDECAR_PORT"
