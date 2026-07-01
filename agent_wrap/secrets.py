# This file has been edited with the assistance of an AI tool.
"""
Secrets storage for agent-wrap.

Encrypted on-disk backend, pure Python stdlib. Encryption key is derived from
three components so copying files between machines or repo clones cannot
decrypt the store:

* **Keyfile** — 32 random bytes at ``<launches>/.secrets-key`` (``0o600``),
  generated on first use.
* **Machine-id** — ``/etc/machine-id`` (stable per-machine UUID).
* **Repo identity** — first commit hash of the agent-wrap clone (``git
  rev-list --max-parents=0 HEAD``).

All three are mixed via a single HMAC-SHA256 to produce the encryption key.
Fallbacks (with a stderr warning) keep the system operable when
``/etc/machine-id`` or git are unavailable.

Encryption is HMAC-SHA256 in CTR mode with encrypt-then-MAC authentication.
The payload is ``nonce(16) || ciphertext || hmac(32)``.
"""

from __future__ import annotations

import contextlib
import functools
import getpass
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

from agent_wrap.constants import AGENT_LAUNCHES_DIR, TOOL_DIR
from agent_wrap.lib.console import Ansi

# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class SecretNotFoundError(Exception):
    """Raised when a required secret is not found in the store."""

    def __init__(self, key: str, description: str) -> None:
        self.key = key
        self.description = description
        super().__init__(f"Secret '{key}' ({description}) not found in secrets store")


# ---------------------------------------------------------------------------
# Key derivation — three-component mixer
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _derive_key() -> bytes:
    """
    Derive the symmetric encryption key, cached in-process.

    Mixes three components via HMAC-SHA256 so an attacker needs all three:

    1. **Keyfile** — 32 random bytes, generated once.
    2. **Machine-id** — ``/etc/machine-id`` (stable per-machine).
    3. **First-commit hash** — from git (stable per clone).
    """
    keyfile_path = _keyfile_path()

    # -- keyfile --
    try:
        keyfile_bytes = keyfile_path.read_bytes()
    except (FileNotFoundError, OSError):
        keyfile_bytes = os.urandom(32)
        keyfile_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(keyfile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(keyfile_bytes)

    h = hmac.new(keyfile_bytes, digestmod=hashlib.sha256)

    # -- machine-id (prevents copy-paste to another computer) --
    try:
        machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        if not machine_id:
            print(
                f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} /etc/machine-id is empty —"
                " secrets are not bound to this machine",
                file=sys.stderr,
            )
    except (FileNotFoundError, OSError):
        print(
            f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} /etc/machine-id not found —"
            " secrets are not bound to this machine",
            file=sys.stderr,
        )
        machine_id = ""
    h.update(machine_id.encode())

    # -- repo identity (prevents copy-paste to another clone) --
    result = subprocess.run(
        ["git", "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(TOOL_DIR),
        check=True,
    )
    repo_identity = result.stdout.strip()
    h.update(repo_identity.encode())

    return h.digest()


def _keyfile_path() -> Path:
    """Path to the random keyfile used in key derivation."""
    return AGENT_LAUNCHES_DIR / ".secrets-key"


# ---------------------------------------------------------------------------
# Encryption primitives (HMAC-SHA256-CTR + encrypt-then-MAC)
# ---------------------------------------------------------------------------


_MIN_PAYLOAD_LEN = 48  # 16 nonce + 0 data + 32 hmac minimum
_NONCE_LEN = 16
_MAC_LEN = 32
_BLOCK_LEN = 32


def _encrypt(plaintext: bytes, key: bytes) -> bytes:
    """
    Encrypt *plaintext* using HMAC-SHA256 in CTR mode.

    Format: ``nonce(16) || ciphertext || hmac(32)``.
    Two sub-keys are derived from *key* for the encryption and authentication
    steps so the same key is never used for both operations.
    """
    nonce = os.urandom(16)

    enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
    auth_key = hmac.new(key, b"auth", hashlib.sha256).digest()

    # CTR mode — generate a keystream block per 32-byte plaintext chunk
    ciphertext = bytearray()
    for i in range(0, len(plaintext), 32):
        block_num = i // 32
        counter = nonce + struct.pack(">Q", block_num)
        keystream = hmac.new(enc_key, counter, hashlib.sha256).digest()
        block = plaintext[i : i + 32]
        for a, b in zip(block, keystream, strict=False):
            ciphertext.append(a ^ b)

    ciphertext_bytes = bytes(ciphertext)
    mac = hmac.new(auth_key, nonce + ciphertext_bytes, hashlib.sha256).digest()
    return nonce + ciphertext_bytes + mac


def _decrypt(payload: bytes, key: bytes) -> bytes | None:
    """
    Decrypt *payload* and verify the authentication tag.

    Returns the plaintext on success, ``None`` on HMAC mismatch or structural
    corruption (too short to contain nonce + MAC).
    """
    if len(payload) < _MIN_PAYLOAD_LEN:
        return None

    nonce = payload[:16]
    mac = payload[-32:]
    ciphertext = payload[16:-32]

    enc_key = hmac.new(key, b"enc", hashlib.sha256).digest()
    auth_key = hmac.new(key, b"auth", hashlib.sha256).digest()

    # Verify HMAC first (constant-time) — don't decrypt if the tag is wrong.
    expected_mac = hmac.new(auth_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        return None

    plaintext = bytearray()
    for i in range(0, len(ciphertext), 32):
        block_num = i // 32
        counter = nonce + struct.pack(">Q", block_num)
        keystream = hmac.new(enc_key, counter, hashlib.sha256).digest()
        block = ciphertext[i : i + 32]
        for a, b in zip(block, keystream, strict=False):
            plaintext.append(a ^ b)

    return bytes(plaintext)


# ---------------------------------------------------------------------------
# Encrypted file read / write
# ---------------------------------------------------------------------------


def _secrets_path() -> Path:
    """Path to the encrypted secrets file."""
    return AGENT_LAUNCHES_DIR / "secrets.enc"


# Path to the old plaintext fallback (pre-encrypted-store era).
_OLD_FALLBACK_PATH: Path = AGENT_LAUNCHES_DIR / "secrets.json"


def _maybe_migrate_old_fallback() -> None:
    """
    One-shot migration from the old plaintext ``secrets.json`` fallback.

    When the old file exists but the new encrypted file does not, copy its
    content into the encrypted store and remove the plaintext file.
    """
    old_path = _OLD_FALLBACK_PATH
    new_path = _secrets_path()
    if not old_path.is_file() or new_path.is_file():
        return

    try:
        data = json.loads(old_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        old_path.unlink(missing_ok=True)
        return

    if not isinstance(data, dict) or not data:
        old_path.unlink(missing_ok=True)
        return

    _write_all({str(k): v for k, v in data.items() if isinstance(v, str)})
    with contextlib.suppress(OSError):
        old_path.unlink(missing_ok=True)


def _read_all() -> dict[str, str]:
    """
    Decrypt and return all stored secrets.

    Returns an empty dict when the file doesn't exist, cannot be decrypted
    (key changed / tampering), or contains invalid JSON.
    """
    path = _secrets_path()
    if not path.is_file():
        return {}

    try:
        ciphertext = path.read_bytes()
    except OSError:
        return {}

    key = _derive_key()
    plaintext = _decrypt(ciphertext, key)
    if plaintext is None:
        print(
            f"{Ansi.BOLD_RED}Warning:{Ansi.RESET} secrets file could not be"
            " decrypted — the encryption key may have changed (machine-id,"
            " repo identity, or keyfile).  Re-run 'agent secrets set <name>'"
            " to re-enter secrets.",
            file=sys.stderr,
        )
        return {}

    try:
        data = json.loads(plaintext)
    except json.JSONDecodeError:
        print(
            f"{Ansi.BOLD_YELLOW}Warning:{Ansi.RESET} secrets file is corrupt —"
            " re-run 'agent secrets set <name>' to re-enter secrets.",
            file=sys.stderr,
        )
        return {}

    if not isinstance(data, dict):
        return {}

    # Skip non-string values — secrets are always strings in practice,
    # and coercing None→"None" or int→str would mask data corruption.
    return {str(k): v for k, v in data.items() if isinstance(v, str)}


def _write_all(data: dict[str, str]) -> None:
    """
    Encrypt *data* and atomically write it to the secrets file.

    Uses a sibling temp file + rename for atomicity; the parent directory is
    created if missing.
    """
    path = _secrets_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = _derive_key()

    plaintext = json.dumps(data, indent=2).encode()
    ciphertext = _encrypt(plaintext, key)

    # Atomic write: temp sibling → rename
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(ciphertext)
        tmp.chmod(0o600)
        tmp.replace(path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read(key: str, description: str, *, prompt_on_missing: bool = False) -> str:
    """
    Return the secret for *key*.

    Looks up *key* in the encrypted secrets store.  If not found and
    *prompt_on_missing* is ``True`` the user is interactively prompted and the
    entered value is persisted before it is returned.

    Raises :class:`SecretNotFoundError` when the key is absent and
    *prompt_on_missing* is ``False``.
    """
    _maybe_migrate_old_fallback()
    data = _read_all()
    value = data.get(key)
    if value is not None:
        return value

    if not prompt_on_missing:
        raise SecretNotFoundError(key, description)

    print(f"Secret: {description}", file=sys.stderr)
    try:
        entered = getpass.getpass("Value: ")
    except EOFError:
        raise SecretNotFoundError(key, description) from None

    data[key] = entered
    _write_all(data)
    return entered


def write(key: str, description: str) -> None:
    """Prompt the user for *key* and persist it to the encrypted store."""
    _maybe_migrate_old_fallback()
    print(f"Secret: {description}", file=sys.stderr)
    entered = getpass.getpass("Value: ")
    data = _read_all()
    data[key] = entered
    _write_all(data)


def delete(key: str) -> None:
    """Remove *key* from the encrypted store.  No-op when absent."""
    _maybe_migrate_old_fallback()
    data = _read_all()
    if key in data:
        del data[key]
        _write_all(data)


def list_keys() -> list[str]:
    """Return all key names currently stored (sorted)."""
    _maybe_migrate_old_fallback()
    return sorted(_read_all().keys())
