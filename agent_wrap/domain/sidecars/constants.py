# This file has been created with the assistance of an AI tool.
"""Constants for the sidecars domain."""

from agent_wrap.constants import BASE_IMAGE_NAME

#: Docker label name used to identify agent containers.
ROLE_LABEL = "agent-wrap.role"
#: Docker label value identifying agent containers.
ROLE_VALUE = BASE_IMAGE_NAME
