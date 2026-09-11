# This file has been edited with the assistance of an AI tool.
"""Constants for `agent stats`."""

import re

from agent_wrap.domain.display.models import TableSpec

#: Matches the relative ``-Nd`` date form accepted by ``--from``/``--until``.
RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")

#: The trailing numeric columns every usage table ends with, in the order
#: :func:`agent_wrap.cli.stats.render.usage_cells` emits them. Shared by all three tables
#: so their figures line up vertically when two are stacked.
USAGE_HEADERS = ("MSGS", "INPUT", "OUTPUT", "CACHE-W", "CACHE-R", "COST")
USAGE_ALIGNS = (">", ">", ">", ">", ">", ">")

#: The per-project tree. ``leading=3`` holds the tree and its two per-project columns back
#: from the shared measurement, which is what lets this table stack with `RECENT_TABLE`.
PROJECTS_TABLE = TableSpec(
    headers=("PROJECT", "SESSIONS", "LAST LAUNCH", *USAGE_HEADERS),
    aligns=("<", ">", "<", *USAGE_ALIGNS),
    leading=3,
)

#: The per-model + per-day table, stacked under `PROJECTS_TABLE`. No SESSIONS or
#: LAST LAUNCH columns, so its label is the only leading one.
RECENT_TABLE = TableSpec(
    headers=("MODEL / DATE", *USAGE_HEADERS),
    aligns=("<", *USAGE_ALIGNS),
    leading=1,
)

#: The verbose usage-source breakdown, rendered standalone after the two stacked tables
#: and so sized on its own content.
SOURCE_TABLE = TableSpec(
    headers=("SOURCE", *USAGE_HEADERS),
    aligns=("<", *USAGE_ALIGNS),
    leading=1,
)
