# This file has been edited with the assistance of an AI tool.
"""Constants for the stats domain subpackage."""

import os
import re

from agent_wrap.lib.daytime import local_utc_offset_hours

# Default span (in days) for the usage window when no explicit count is given.
DEFAULT_DAYS = 28

# Hours in a day -- AGENT_DAY_START_UTC must fall strictly within (-HOURS_PER_DAY, HOURS_PER_DAY).
HOURS_PER_DAY = 24


def _parsed_day_start_hours() -> int:
    raw = os.environ.get("AGENT_DAY_START_UTC")
    if not raw:
        return -local_utc_offset_hours()
    value = int(raw)  # raises ValueError on malformed input -- let it propagate
    if abs(value) >= HOURS_PER_DAY:
        msg = f"AGENT_DAY_START_UTC must satisfy -24 < value < 24, got {value!r}"
        raise ValueError(msg)
    return value


# Hours past UTC midnight at which a stats "day" begins (may be negative, but
# must satisfy -24 < value < 24). Defaults to the host's local midnight;
# override with AGENT_DAY_START_UTC.
DAY_START_HOURS = _parsed_day_start_hours()

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
