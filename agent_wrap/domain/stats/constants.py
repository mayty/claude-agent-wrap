# This file has been edited with the assistance of an AI tool.
"""Constants for the stats domain subpackage."""

import re

# Default span (in days) for the usage window when no explicit count is given.
DEFAULT_DAYS = 28

RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")

VALUE_FLAGS = ("-f", "--from", "-u", "--until")

# Marker filename used to collapse subdirectories into a single aggregated
# "leaf" project in stats and logs viewer output.
MARKER_NAME = ".agent_stats_leaf"

# Display label for orphaned sessions — logs from deleted or unregistered
# projects that no longer have an entry in the project registry.
ORPHANED_LABEL = "<orphaned>"

# The shared sidecar writes every project's logs under
# ``<tool_dir>/litellm-logs/<project_hash>/<provider>/<session>/``.
CENTRAL_LOGS_DIRNAME = "litellm-logs"
