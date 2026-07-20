# This file has been created with the assistance of an AI tool.
"""Constants for the config domain subpackage."""

import re

# Matches a {N}/ prefix at the start of a line (N = non-zero digits).
PREFIX_RE = re.compile(r"^\{(\d+)\}/(.*)$")

# Matches a terminal /{...} sibling group containing at least one comma.
SIBLING_RE = re.compile(r"/\{([^}]+)\}$")

# Minimum run length to trigger sibling grouping.
MIN_SIBLING_RUN = 2
