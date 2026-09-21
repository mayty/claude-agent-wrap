# This file has been created with the assistance of an AI tool.

from agent_wrap.domain.display.models import TableSpec

#: The check table. STATE is a column rather than a stream split: severity stays on the
#: verdict line, so a row states presence in line and nothing about a row is indented past
#: its neighbours. SIDECAR is a column too, so a sidecar is named once per group of its keys
#: rather than spelled into each one.
#:
#: ``leading=0``, like the cleanup table and unlike the inspect ones: there is one table
#: here and no path tree to hold back, so every column is measured with the rest.
#:
#: Nothing is elidable. LENGTH and HINT are figures, and half a figure reads as a wrong one;
#: the key and the sidecar name are identifiers, and half of one is a different one.
SECRETS_CHECK_TABLE = TableSpec(
    headers=("SIDECAR", "SECRET", "STATE", "LENGTH", "HINT"),
    aligns=("<", "<", "<", ">", "<"),
    leading=0,
)

#: The removal table, shared by `clear` and `cleanup`. Their titles carry which of the two
#: ran, so the STATE cell does not have to.
SECRETS_REMOVED_TABLE = TableSpec(
    headers=("SIDECAR", "SECRET", "STATE"),
    aligns=("<", "<", "<"),
    leading=0,
)

#: Title of the check table, in both modes.
SECRETS_CHECK_TITLE = "Secrets:"

#: SIDECAR cell for a stored key with no `:` to split. `cleanup` prints one whenever it
#: removes a key no known sidecar claims; an empty cell would read as a rendering fault.
NO_SIDECAR = "<none>"

#: Titles of the removal table, formatted with ``sidecar`` and ``count``.
SECRETS_CLEAR_TITLE = "Removed from '{sidecar}' ({count}):"
SECRETS_CLEANUP_TITLE = "Unknown keys removed ({count}):"

# Cells of the STATE column.
STATE_OK = "OK"
STATE_MISSING = "MISSING"
STATE_REMOVED = "REMOVED"
