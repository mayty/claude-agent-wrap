# This file has been edited with the assistance of an AI tool.
"""Constants for the stats domain subpackage."""

# Marker filename used to collapse subdirectories into a single aggregated
# "leaf" project in stats and logs viewer output.
MARKER_NAME = ".agent_stats_leaf"

# Stand-in date/hour key for records whose timestamp could not be read (the
# callback failed to stamp ``timing.start``). Shared by the scanner, the range
# filter, and the archive, so these records stay visible in the all-time view and
# are excluded from any bounded window.
UNKNOWN_TIME_KEY = "?"

# ``agent cleanup`` archives the usage of orphaned log dirs here before deleting
# them, so their spend keeps showing up in ``agent stats``. Lives beside
# ``projects.txt`` in ``AGENT_LAUNCHES_DIR`` — host-wide bookkeeping that must
# outlive the log dirs it describes.
ORPHANED_ARCHIVE_FILENAME = "orphaned-usage-archive.json"

# Default span (in days) of the usage window when no explicit count is given.
DEFAULT_DAYS = 28
