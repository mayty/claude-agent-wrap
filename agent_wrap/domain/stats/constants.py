# This file has been edited with the assistance of an AI tool.
"""Constants for the stats domain subpackage."""

# Marker filename used to collapse subdirectories into a single aggregated
# "leaf" project in stats and logs viewer output.
MARKER_NAME = ".agent_stats_leaf"

# Display label for orphaned sessions — logs from deleted or unregistered
# projects that no longer have an entry in the project registry.
ORPHANED_LABEL = "<orphaned>"

# The shared sidecar writes every project's logs under
# ``<tool_dir>/litellm-logs/<project_hash>/<provider>/<session>/``.
CENTRAL_LOGS_DIRNAME = "litellm-logs"

# ``agent cleanup`` archives the usage of orphaned log dirs here before deleting
# them, so their spend keeps showing up in ``agent stats``. Lives beside
# ``projects.txt`` in ``AGENT_LAUNCHES_DIR`` — host-wide bookkeeping that must
# outlive the log dirs it describes.
ORPHANED_ARCHIVE_FILENAME = "orphaned-usage-archive.json"
