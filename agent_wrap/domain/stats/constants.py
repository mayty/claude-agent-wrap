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

# Default span (in days) of the usage window when no explicit count is given.
DEFAULT_DAYS = 28

# The hour buckets the usage index is keyed by, and the number of them a day spans.
# Both are here rather than inline because the same two figures convert a window in
# either direction — a stats day to a bucket range, and a bucket back to an instant.
SECONDS_PER_HOUR = 3600
HOURS_PER_DAY = 24
