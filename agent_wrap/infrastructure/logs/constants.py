# This file has been created with the assistance of an AI tool.
"""Constants for the logs database."""

from typing import Final

# The two `blobs.codec` values, matching the CHECK constraint in 0001.
CODEC_RAW: Final = "raw"
CODEC_ZLIB: Final = "zlib"

# Smallest payload worth compressing. Below this zlib's header and trailer cost more
# than they save, and most short payloads are `hash:` pointers that are already digests.
ZLIB_MIN_BYTES: Final = 512

# zlib level for stored payloads. Unlike the viewer's gzip -- which trades ratio for
# latency because it runs per request -- this runs once per distinct blob and is then
# read back many times, so the ratio is what matters.
ZLIB_LEVEL: Final = 6

# `array` typecode for a packed blob-id vector. 'I' is unsigned 4-byte on every platform
# CPython supports, which caps a database at 4.29 billion blobs -- four orders of
# magnitude beyond the ~323k the current tree produces.
BLOB_ID_TYPECODE: Final = "I"

# Largest number of bound parameters in one `IN (...)` list. SQLite's own default limit
# is 32766, so this leaves generous headroom while still keeping the statement small
# enough to parse quickly.
MAX_QUERY_PARAMETERS: Final = 5000
