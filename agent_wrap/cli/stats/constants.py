# This file has been created with the assistance of an AI tool.
"""Constants for `agent stats` argument parsing."""

import re

#: Matches the relative ``-Nd`` date form accepted by ``--from``/``--until``.
RELATIVE_DATE_RE = re.compile(r"^-(\d+)d$")

#: Flags whose ``-Nd`` value must be glued on with ``=`` before argparse sees it.
VALUE_FLAGS = ("-f", "--from", "-u", "--until")

#: Usage prefix for `agent stats`, also printed on a usage error.
USAGE_LINE = (
    "Usage: agent stats [-v|--verbose] [-p|--pattern P] [-f|--from D] [-u|--until D] [-d|--days N]"
)

#: Long-form help printed for `agent stats -h`.
USAGE_TEXT = (
    f"{USAGE_LINE}\n\n"
    "Prints aggregated usage stats from the .claude/litellm-logs/ directory of\n"
    "every project in the registry.\n\n"
    "Output is a per-project table plus a per-model and per-day breakdown,\n"
    "both over the same usage window. Models are displayed as <provider>/<model>.\n"
    "Day buckets use host-local time by default; override with AGENT_DAY_START_UTC.\n\n"
    "Selection range (at most two of --from/--until/--days may be combined):\n"
    "  -f, --from D    inclusive lower bound; D is YYYY-MM-DD or -Nd (e.g. -14d)\n"
    "  -u, --until D   inclusive upper bound; same format as --from\n"
    "  -d, --days N    span in days; N=0 means unlimited (no day bound)\n"
    "Defaults: no flags → last 28 days; --from alone → [from, now];\n"
    "--days N alone → last N days [now-(N-1), now]; --until alone → 28 days\n"
    "ending at until; --days 0 alone → all time [open, now].\n\n"
    "Project filtering:\n"
    "  -p, --pattern P  regex matched against each project's recorded registry\n"
    '                   path (e.g. "api", "my-proj", "/home/me/work/")\n\n'
    "-v/--verbose adds a usage-source breakdown table over the same window,\n"
    "splitting totals by how each record's usage was obtained\n"
    "(native response vs. standard_logging_object recovery vs. unrecoverable).\n\n"
    "Pricing is fetched dynamically per-provider as logs are scanned.\n\n"
    "Projects are recorded by `agent` on each launch — a project that\n"
    "has never had `agent` invoked from it will not appear here."
)
