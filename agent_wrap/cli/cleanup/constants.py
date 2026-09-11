# This file has been created with the assistance of an AI tool.
"""Constants for `agent cleanup`."""

# Spinner label shown while scanning for orphaned dirs and while cleaning up.
CLEANUP_LABEL = "cleanup"

#: Columns of the outdated-images table. The image leads because it is what `docker rmi`
#: would name; SIZE is docker's own figure per image and is deliberately never totalled,
#: since images share layers and a sum would overstate the reclaim.
CLEANUP_IMAGE_HEADERS = ("IMAGE", "SIZE", "WHY")
CLEANUP_IMAGE_ALIGNS = ("<", ">", "<")

#: Columns of that table which may be cut short on a narrow console: the image reference
#: (a `repo@sha256:...` runs long) and the reason prose. SIZE is absent -- a truncated size
#: reads as a wrong figure rather than a shortened one.
CLEANUP_IMAGE_ELIDE = (0, 2)

#: Title of that table, formatted with ``count``.
CLEANUP_IMAGE_TITLE = "Outdated images ({count}):"

#: Printed under the table whenever it holds a stale project image. A note rather than part
#: of the group heading because the heading sits in the elidable IMAGE column, and this is
#: the one line in the preview that says what confirming will *cost* rather than reclaim.
STALE_REBUILD_NOTE = (
    "Each stale project image is rebuilt on that project's next 'agent run' -- which that "
    "project already owed, since a launch would have rebuilt it anyway."
)

#: Printed when untagged images exist that carry no `agent-wrap.image` label. They are
#: never removed: a wrapper build from before the label existed is indistinguishable from a
#: leftover of the user's own `docker build`, so the pointer goes to the tool that does not
#: have to tell them apart. Formatted with ``count``.
UNATTRIBUTABLE_NOTE = (
    "{count} untagged image(s) carry no agent-wrap label, so they cannot be attributed "
    "to the wrapper and are left alone -- 'docker image prune' removes them."
)

#: Appended to the skipped-image warning. Docker refuses to remove an image a container
#: still references and `agent cleanup` never forces, so this is nearly always the reason.
SKIPPED_IMAGE_NOTE = "docker refused to remove (usually still in use by a container)"

#: Printed in the preview, on every real run. The sweep is not part of the surveyed
#: scope: finding out how much it would reclaim costs the same full pass as reclaiming
#: it, so this states that it happens rather than what it will find.
INDEX_SWEEP_NOTE = (
    "Content in the request index that no request reaches any more will be reclaimed, "
    "and the pages returned to the database file."
)

#: Printed when the reclaim found the ingest lock held. Not a failure -- everything else
#: in the run happened, and the content is exactly as reachable as it was before. One
#: message for both halves because they are one lock: retention and the sweep are either
#: both done or both skipped.
INDEX_RECLAIM_SKIPPED = (
    "The request index was not swept: another process is ingesting. Nothing was lost -- "
    "re-run 'agent cleanup' to reclaim it."
)

#: Preview line for retention, formatted with ``count``, ``days`` and ``size``. Surveyed
#: rather than merely announced, unlike the sweep above: this one deletes log files, so
#: what it would take has to be on screen before the prompt.
RETENTION_NOTE = (
    "{count} indexed session log(s) have had no activity for {days} day(s) and will be "
    "deleted, freeing ~{size}."
)

#: Printed under it. The consequence is the whole reason AGENT_LOGS_RETENTION_DAYS is
#: off by default, so the run that acts on it says out loud what is being given up.
RETENTION_CONSEQUENCE = (
    "Their log files go with their index rows, so their spend leaves 'agent stats' "
    "permanently. Unset AGENT_LOGS_RETENTION_DAYS to switch retention off."
)
