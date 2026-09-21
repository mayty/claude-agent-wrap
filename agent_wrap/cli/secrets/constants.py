# This file has been created with the assistance of an AI tool.

from agent_wrap.domain.display.models import TableSpec

#: The check table. STATE is a column rather than a stream split: severity stays on the
#: verdict line, so a row states presence in line and nothing about a row is indented past
#: its neighbours.
#:
#: ``leading=0``, like the cleanup table and unlike the inspect ones: there is one table
#: here and no path tree to hold back, so every column is measured with the rest.
#:
#: Nothing is elidable. LENGTH and HINT are figures, and half a figure reads as a wrong
#: one; the key is an identifier, and half of one is a different key. Nor would it buy
#: much: STATE is 7 wide, HINT at most 9, LENGTH 6, so even a 40-character key leaves the
#: table inside 80 columns.
SECRETS_CHECK_TABLE = TableSpec(
    headers=("SECRET", "STATE", "LENGTH", "HINT"),
    aligns=("<", "<", ">", "<"),
    leading=0,
)

#: The removal table, shared by `clear` and `cleanup`. Their titles carry which of the two
#: ran, so the STATE cell does not have to.
SECRETS_REMOVED_TABLE = TableSpec(
    headers=("SECRET", "STATE"),
    aligns=("<", "<"),
    leading=0,
)

#: Title of the check table, formatted with ``sidecar``. No count: it would restate the
#: verdict line's arithmetic on a failure and say nothing on a pass.
SECRETS_CHECK_TITLE = "Secrets for '{sidecar}':"

#: Titles of the removal table, formatted with ``sidecar`` and ``count``.
SECRETS_CLEAR_TITLE = "Removed from '{sidecar}' ({count}):"
SECRETS_CLEANUP_TITLE = "Unknown keys removed ({count}):"

# Cells of the STATE column.
STATE_OK = "OK"
STATE_MISSING = "MISSING"
STATE_REMOVED = "REMOVED"
