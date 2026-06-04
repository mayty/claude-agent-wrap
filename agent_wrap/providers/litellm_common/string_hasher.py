# This file has been created with the assistance of an AI tool.
"""
String hasher for deduplicating repeated strings in LiteLLM logs.

This module provides a StringHasher class that converts long strings into
"hash:<sha256_hex>" format to reduce space bloat in log files. Strings shorter
than 66 characters are left unchanged since the hash format would consume more
space than the original string.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

# Global cache of hashers per session to enable cross-request deduplication
# and prevent concurrent flushes from writing duplicate mappings.
_SESSION_HASHERS: dict[str, StringHasher] = {}


def get_session_hasher(session_id: str) -> StringHasher:
    """Get or create a StringHasher for a specific session, loading existing state."""
    if session_id not in _SESSION_HASHERS:
        hasher = StringHasher()
        hasher.load_seen_hashes(session_id)
        _SESSION_HASHERS[session_id] = hasher
    return _SESSION_HASHERS[session_id]


class StringHasher:
    """
    Manages per-session string-to-hash mappings for log deduplication.

    Tracks seen strings and replaces them with "hash:<sha256_hex>" format.
    Maintains reverse mapping for persistence to strings.json.
    """

    # Threshold: len("hash:") + 64 (SHA-256 hex length) = 66
    HASH_PREFIX = "hash:"
    MIN_LENGTH = len(HASH_PREFIX) + 64 + 1

    def __init__(self) -> None:
        self._strings_to_hashes: dict[str, str] = {}
        self._hashes_to_strings: dict[str, str] = {}
        self._seen_hashes: set[str] = set()

    def load_seen_hashes(self, session_id: str) -> None:
        """
        Load existing hashes from strings.jsonl to prevent duplicate writes
        after a sidecar restart, without loading full strings into memory.
        """
        log_dir = Path(f"/var/log/agent-wrap/{session_id}")
        strings_file = log_dir / "strings.jsonl"

        if not strings_file.exists():
            return

        try:
            with strings_file.open("r", encoding="utf-8") as f:
                content = f.read()

            for line in content.splitlines():
                try:
                    entry = json.loads(line)
                    if "hash" in entry:
                        self._seen_hashes.add(entry["hash"])
                except json.JSONDecodeError:
                    # Skip malformed lines
                    continue
        except OSError:
            # If we can't read the file, just start with an empty set
            pass

    def hash_string(self, s: str) -> str:
        """
        Hash a string if it meets the minimum length threshold.

        Args:
            s: The string to potentially hash

        Returns:
            "hash:<sha256_hex>" if len(s) >= MIN_LENGTH, else the original string

        """
        if len(s) < self.MIN_LENGTH:
            return s

        # Check if we've already hashed this string
        if s in self._strings_to_hashes:
            return self._strings_to_hashes[s]

        # Create new hash
        hash_value = f"{self.HASH_PREFIX}{hashlib.sha256(s.encode('utf-8')).hexdigest()}"

        # Store in both directions for O(1) lookup and reverse mapping
        self._strings_to_hashes[s] = hash_value
        self._hashes_to_strings[hash_value] = s

        return hash_value

    def flush(self, session_id: str) -> None:
        """
        Append accumulated string mappings to strings.jsonl.

        Using JSONL allows pure append operations, avoiding the need to read
        and rewrite the entire file on every flush. We check _seen_hashes to
        prevent duplicate writes after a sidecar restart.

        Args:
            session_id: The session identifier used to construct the file path

        """
        if not self._hashes_to_strings:
            return

        # Atomically grab and clear the mappings to prevent concurrent flushes
        # (from asyncio or threads) from writing the same data twice.
        # Any new strings hashed during the write will go into the new dict
        # and be caught by the next flush.
        mappings_to_write = self._hashes_to_strings
        self._hashes_to_strings = {}

        log_dir = Path(f"/var/log/agent-wrap/{session_id}")
        strings_file = log_dir / "strings.jsonl"

        # Ensure directory exists
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # In environments where we can't create the directory (e.g., tests),
            # skip flushing but don't break the logging.
            # Restore mappings since we failed to flush.
            self._hashes_to_strings.update(mappings_to_write)
            return

        # Append new mappings as individual JSON lines, skipping duplicates
        try:
            with strings_file.open("a", encoding="utf-8") as f:
                for h, s in mappings_to_write.items():
                    if h not in self._seen_hashes:
                        f.write(json.dumps({"hash": h, "original": s}) + "\n")
                        self._seen_hashes.add(h)
        except OSError as e:
            # Best-effort logging - don't break the proxy if we can't write.
            # Restore mappings so they can be attempted on the next flush.
            self._hashes_to_strings.update(mappings_to_write)
            print(f"agent-wrap callback: failed to append to strings.jsonl: {e}", flush=True)
