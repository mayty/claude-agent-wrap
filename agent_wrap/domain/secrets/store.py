# This file has been created with the assistance of an AI tool.
"""
Encrypted on-disk storage for agent-wrap secrets.

The payload is a Fernet token -- AES-128-CBC under an encrypt-then-MAC HMAC-SHA256 tag,
keyed by an HKDF expansion of :meth:`KeyDerivation.derive_key`'s master key.
"""

import base64
import functools
import hashlib
import hmac
import json
import os
import struct
import subprocess
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent_wrap.constants import TOOL_DIR
from agent_wrap.domain.secrets.constants import (
    FERNET_SUBKEY_LABEL,
    LEGACY_AUTH_SUBKEY_LABEL,
    LEGACY_ENCRYPTION_SUBKEY_LABEL,
    LEGACY_MIN_PAYLOAD_LEN,
    SECRETS_ENCRYPTED_FILE_PATH,
    SECRETS_KEYFILE_PATH,
)
from agent_wrap.lib.atomic import atomic_write_bytes

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


class KeyDerivation:
    """
    Three-component HMAC-SHA256 key derivation for the secrets store.

    A 32-byte keyfile, ``/etc/machine-id``, and the repo's first-commit hash -- an
    attacker needs all three.
    """

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def derive_key(display: DisplayService) -> bytes:
        """
        Derive the symmetric encryption key.

        The keyfile is mixed with ``/etc/machine-id`` and the repo's root commit, so a
        copied secrets file is undecryptable on another computer or in another clone.
        """
        keyfile_path = SECRETS_KEYFILE_PATH

        try:
            keyfile_bytes = keyfile_path.read_bytes()
        except FileNotFoundError, OSError:
            keyfile_bytes = os.urandom(32)
            keyfile_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(keyfile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(keyfile_bytes)

        h = hmac.new(keyfile_bytes, digestmod=hashlib.sha256)

        try:
            machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
            if not machine_id:
                display.warning("/etc/machine-id is empty — secrets are not bound to this machine")
        except FileNotFoundError, OSError:
            display.warning("/etc/machine-id not found — secrets are not bound to this machine")
            machine_id = ""
        h.update(machine_id.encode())

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


def _fernet(key: bytes) -> Fernet:
    """
    Build the cipher from *key*, the master :meth:`KeyDerivation.derive_key` returns.

    The HKDF expansion gives the token key a domain of its own, so the master is never
    itself cipher key material -- it is still the HMAC key of the superseded format
    :func:`_decrypt_legacy` reads.
    """
    token_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=FERNET_SUBKEY_LABEL
    ).derive(key)
    return Fernet(base64.urlsafe_b64encode(token_key))


def _decrypt_legacy(payload: bytes, key: bytes) -> bytes | None:
    """
    Read the superseded ``nonce(16) || HMAC-SHA256-CTR ciphertext || mac(32)`` payload.

    Here because that is the format of every ``secrets.enc`` written before 0.11.0.
    :meth:`EncryptedFileStore.read_all` rewrites such a file as a Fernet token the first
    time it reads one, so this goes one release after that upgrade has had a chance to
    run everywhere. ``None`` on a wrong key, a bad tag, or a payload too short to hold
    both the nonce and the tag.
    """
    if len(payload) < LEGACY_MIN_PAYLOAD_LEN:
        return None

    nonce, ciphertext, mac = payload[:16], payload[16:-32], payload[-32:]
    enc_key = hmac.new(key, LEGACY_ENCRYPTION_SUBKEY_LABEL, hashlib.sha256).digest()
    auth_key = hmac.new(key, LEGACY_AUTH_SUBKEY_LABEL, hashlib.sha256).digest()

    expected_mac = hmac.new(auth_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expected_mac):
        return None

    # CTR mode, chunked at the SHA-256 digest length so each HMAC output covers exactly
    # one chunk; ``enumerate`` yields the block index the counter is packed from. Both
    # ``strict=False`` flags are the short trailing chunk: ``batched`` must yield it
    # rather than raise, and the surplus keystream over it must be dropped.
    plaintext = bytearray()
    for block_num, block in enumerate(batched(ciphertext, 32, strict=False)):
        counter = nonce + struct.pack(">Q", block_num)
        keystream = hmac.new(enc_key, counter, hashlib.sha256).digest()
        for a, b in zip(block, keystream, strict=False):
            plaintext.append(a ^ b)

    return bytes(plaintext)


class EncryptedFileStore:
    """Encrypted file read/write with atomic replacement."""

    @staticmethod
    def read_all(display: DisplayService) -> dict[str, str]:
        """
        Decrypt and return all stored secrets.

        Empty when the file is absent, undecryptable, or not valid JSON.
        """
        path = SECRETS_ENCRYPTED_FILE_PATH
        if not path.is_file():
            return {}

        try:
            payload = path.read_bytes()
        except OSError:
            return {}

        key = KeyDerivation.derive_key(display)
        superseded = False
        try:
            plaintext = _fernet(key).decrypt(payload)
        except InvalidToken:
            plaintext = _decrypt_legacy(payload, key)
            superseded = True

        if plaintext is None:
            display.warning(
                "secrets file could not be decrypted — the encryption key may have"
                " changed (machine-id, repo identity, or keyfile).  Re-run"
                " 'agent secrets set <name>' to re-enter secrets."
            )
            return {}

        try:
            data = json.loads(plaintext)
        except json.JSONDecodeError:
            display.warning(
                "secrets file is corrupt — re-run 'agent secrets set <name>' to re-enter secrets."
            )
            return {}

        if not isinstance(data, dict):
            return {}

        # Skip non-string values — secrets are always strings in practice,
        # and coercing None→"None" or int→str would mask data corruption.
        secrets = {str(k): v for k, v in data.items() if isinstance(v, str)}

        # Rewrite on read, not on the next write: nothing guarantees a `secrets set` ever
        # follows, and the superseded reader above goes away a release from now.
        if superseded:
            EncryptedFileStore.write_all(secrets, display=display)
        return secrets

    @staticmethod
    def write_all(data: dict[str, str], *, display: DisplayService) -> None:
        """Encrypt *data* and atomically write it to the secrets file."""
        plaintext = json.dumps(data, indent=2).encode()
        atomic_write_bytes(
            SECRETS_ENCRYPTED_FILE_PATH,
            _fernet(KeyDerivation.derive_key(display)).encrypt(plaintext),
            mode=0o600,
        )
