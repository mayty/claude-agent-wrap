# This file has been edited with the assistance of an AI tool.
"""Constants for `agent stats` argument parsing."""

import re

#: Matches the relative ``-Nd`` date form accepted by ``--from``/``--until``.
RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")
