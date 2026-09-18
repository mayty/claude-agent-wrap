# This file has been created with the assistance of an AI tool.
"""
Canonical JSON encoding, and the content address derived from it.

The logs database addresses every stored value by the SHA-256 of its canonical bytes,
which is what lets 3.1M message references collapse to ~118k rows. Dedup is therefore
only as good as this encoding is stable: two spellings of the same value must produce
the same bytes, and two different values must not.

**This encoding is a wire format, not an implementation detail.** Changing it corrupts
nothing, but it silently *splits* dedup: every value re-observed afterwards is stored
again under a new address, and nothing fails -- the database just quietly grows. That is
why the tests pin real byte sequences rather than round-trip behaviour alone.

Values are hashed **as they appear in the log file, with ``hash:`` pointers left
unresolved**. That is cheaper than resolving them, and it is what makes an address
stable across sessions: the sidecar's ``StringHasher`` computes an unsalted global
SHA-256, so the same interned string carries the same pointer in every session.
"""

import hashlib
import json


def canonical_bytes(value: object) -> bytes:
    r"""
    Encode *value* to the canonical byte string used for content addressing.

    Three choices, each load-bearing:

    * ``sort_keys=True`` -- object key order is not semantic in JSON, so two records
      differing only in key order must share an address.
    * ``separators=(",", ":")`` -- no insignificant whitespace.
    * ``ensure_ascii=False`` -- non-ASCII is emitted as UTF-8 rather than ``\\uXXXX``
      escapes. Conversation text is heavily non-ASCII, and escaping would inflate it.

    ``surrogatepass`` because JSON permits unpaired surrogates and ``json.loads`` hands
    one back as-is; strict UTF-8 would turn one malformed model response into a crashed
    ingest pass. It is byte-identical for every input without a lone surrogate.
    """
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return encoded.encode("utf-8", errors="surrogatepass")


def content_address(value: object) -> bytes:
    """
    Return the 32-byte SHA-256 of *value*'s canonical bytes.

    Raw digest rather than hex: it is stored in a ``BLOB`` column constrained to
    ``length(sha256) = 32``, and hex would double the index.
    """
    return hashlib.sha256(canonical_bytes(value)).digest()
