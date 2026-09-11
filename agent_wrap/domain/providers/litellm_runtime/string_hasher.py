# This file has been created with the assistance of an AI tool.
"""
String hasher for deduplicating repeated strings in LiteLLM logs.

Strings shorter than 66 characters are left unchanged: below that the
``"hash:<sha256_hex>"`` replacement is longer than the original.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


class StringHasher:
    """Per-session string-to-hash mappings, persisted to ``strings.jsonl``."""

    HASH_PREFIX = "hash:"
    MIN_LENGTH = len(HASH_PREFIX) + 64 + 1

    def __init__(self) -> None:
        self._strings_to_hashes: dict[str, str] = {}
        self._hashes_to_strings: dict[str, str] = {}
        self._seen_hashes: set[str] = set()

    def load_seen_hashes(self, log_dir: Path) -> None:
        """
        Load the hashes already in ``strings.jsonl``, without their strings.

        Only the hashes, so a sidecar restart does not re-append mappings the file
        already holds at the cost of holding every string in memory.

        *log_dir* must be the directory the callback resolved (``_get_log_dir``);
        it is not reconstructible from the session id.
        """
        strings_file = log_dir / "strings.jsonl"

        if not strings_file.exists():
            return

        try:
            with strings_file.open("r", encoding="utf-8") as f:
                content = f.read()

            for line in content.splitlines():
                entry = self._try_parse_json(line)
                if entry is not None and "hash" in entry:
                    self._seen_hashes.add(entry["hash"])
        except OSError:
            pass

    def _try_parse_json(self, line: str) -> Any:
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            return None

    def hash_string(self, s: str) -> str:
        """Hash *s* if it meets :attr:`MIN_LENGTH`, else return it unchanged."""
        if len(s) < self.MIN_LENGTH:
            return s

        if s in self._strings_to_hashes:
            return self._strings_to_hashes[s]

        hash_value = f"{self.HASH_PREFIX}{hashlib.sha256(s.encode('utf-8')).hexdigest()}"

        self._strings_to_hashes[s] = hash_value
        self._hashes_to_strings[hash_value] = s

        return hash_value

    def flush(self, log_dir: Path) -> None:
        """
        Append accumulated string mappings to ``strings.jsonl``.

        JSONL so a flush is a pure append rather than a read-modify-write of the
        whole file.
        """
        if not self._hashes_to_strings:
            return

        # Grab and clear in one step: a concurrent flush (asyncio or thread) would
        # otherwise write the same mappings twice. Strings hashed during the write
        # land in the new dict and go out with the next flush.
        mappings_to_write = self._hashes_to_strings
        self._hashes_to_strings = {}

        strings_file = log_dir / "strings.jsonl"

        try:
            log_dir.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            # Restored, not dropped: the next flush retries them.
            self._hashes_to_strings.update(mappings_to_write)
            return

        try:
            with strings_file.open("a", encoding="utf-8") as f:
                for h, s in mappings_to_write.items():
                    if h not in self._seen_hashes:
                        f.write(json.dumps({"hash": h, "original": s}) + "\n")
                        self._seen_hashes.add(h)
        except OSError as e:
            # Never fatal: a proxy that cannot write its log still proxies.
            self._hashes_to_strings.update(mappings_to_write)
            print(f"agent-wrap callback: failed to append to strings.jsonl: {e}", flush=True)
