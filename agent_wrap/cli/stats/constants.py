# This file has been created with the assistance of an AI tool.
"""Constants for `agent stats` argument parsing."""

import re

# Default span (in days) for the usage window when no explicit count is given.
DEFAULT_DAYS = 28

RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")

VALUE_FLAGS = ("-f", "--from", "-u", "--until")
