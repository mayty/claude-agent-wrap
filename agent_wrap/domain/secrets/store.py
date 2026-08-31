# This file has been created with the assistance of an AI tool.
"""
Encrypted on-disk storage for agent-wrap secrets.

Encryption is HMAC-SHA256 in CTR mode with encrypt-then-MAC authentication.
The payload is ``nonce(16) || ciphertext || hmac(32)``.
"""

import contextlib
import functools
import hashlib
import hmac
import json
import os
import struct
import subprocess
import tempfile
from itertools import batched
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agent_wrap.constants import TOOL_DIR
from agent_wrap.domain.secrets.constants import (
    AUTH_SUBKEY_LABEL,
    ENCRYPTION_SUBKEY_LABEL,
    OLD_SECRETS_PATH,
    SECRETS_ENCRYPTED_FILE_PATH,
    SECRETS_KEYFILE_PATH,
)

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService


class KeyDerivation:
    """
    Three-component HMAC-SHA256 key derivation for the secrets store.

    Mixes three components so an attacker needs all three to derive the key:
    1. Keyfile — 32 random bytes, generated once per machine.
    2. Machine-id — ``/etc/machine-id`` (stable per-machine).
    3. First-commit hash — from git (stable per clone).
    """

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def derive_key(display: DisplayService) -> bytes:
        """Derive the symmetric encryption key."""
        keyfile_path = SECRETS_KEYFILE_PATH

        # -- keyfile --
        try:
            keyfile_bytes = keyfile_path.read_bytes()
        except FileNotFoundError, OSError:
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
                display.warning("/etc/machine-id is empty — secrets are not bound to this machine")
        except FileNotFoundError, OSError:
            display.warning("/etc/machine-id not found — secrets are not bound to this machine")
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


class EncryptionPrimitives:
    """HMAC-SHA256-CTR + encrypt-then-MAC for the secrets store."""

    MIN_PAYLOAD_LEN = 48  # 16 nonce + 0 data + 32 hmac minimum

    @staticmethod
    def encrypt(plaintext: bytes, key: bytes) -> bytes:
        """
        Encrypt *plaintext* using HMAC-SHA256 in CTR mode.

        Format: ``nonce(16) || ciphertext || hmac(32)``.
        Two sub-keys are derived from *key* for the encryption and
        authentication steps so the same key is never used for both operations.
        """
        nonce = os.urandom(16)

        enc_key = hmac.new(key, ENCRYPTION_SUBKEY_LABEL, hashlib.sha256).digest()
        auth_key = hmac.new(key, AUTH_SUBKEY_LABEL, hashlib.sha256).digest()

        # CTR mode — one keystream block per chunk, chunked at the SHA-256 digest
        # length so each HMAC output covers exactly one chunk. ``enumerate`` yields
        # the block index the counter is packed from. ``batched`` gives tuples of
        # ints rather than bytes, which the xor below is indifferent to.
        ciphertext = bytearray()
        # batched(strict=False): the trailing chunk is short whenever the payload is
        # not a multiple of 32; yield it as-is rather than raise or pad.
        for block_num, block in enumerate(batched(plaintext, 32, strict=False)):
            counter = nonce + struct.pack(">Q", block_num)
            keystream = hmac.new(enc_key, counter, hashlib.sha256).digest()
            # zip(strict=False): that trailing chunk is shorter than the full 32-byte
            # keystream block, and the surplus keystream must be dropped.
            for a, b in zip(block, keystream, strict=False):
                ciphertext.append(a ^ b)

        ciphertext_bytes = bytes(ciphertext)
        mac = hmac.new(auth_key, nonce + ciphertext_bytes, hashlib.sha256).digest()
        return nonce + ciphertext_bytes + mac

    @staticmethod
    def decrypt(payload: bytes, key: bytes) -> bytes | None:
        """
        Decrypt *payload* and verify the authentication tag.

        Returns the plaintext on success, ``None`` on HMAC mismatch or
        structural corruption (too short to contain nonce + MAC).
        """
        if len(payload) < EncryptionPrimitives.MIN_PAYLOAD_LEN:
            return None

        nonce = payload[:16]
        mac = payload[-32:]
        ciphertext = payload[16:-32]

        enc_key = hmac.new(key, ENCRYPTION_SUBKEY_LABEL, hashlib.sha256).digest()
        auth_key = hmac.new(key, AUTH_SUBKEY_LABEL, hashlib.sha256).digest()

        # Verify HMAC first (constant-time) — don't decrypt if the tag is wrong.
        expected_mac = hmac.new(auth_key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            return None

        plaintext = bytearray()
        for block_num, block in enumerate(batched(ciphertext, 32, strict=False)):
            counter = nonce + struct.pack(">Q", block_num)
            keystream = hmac.new(enc_key, counter, hashlib.sha256).digest()
            for a, b in zip(block, keystream, strict=False):
                plaintext.append(a ^ b)

        return bytes(plaintext)


class EncryptedFileStore:
    """Encrypted file read/write with atomic replacement and old-format migration."""

    @staticmethod
    @functools.lru_cache(maxsize=1)
    def maybe_migrate_old_fallback(  # noqa: C901, PLR0912, PLR0915
        display: DisplayService,
    ) -> None:
        """
        Migrate old ``~/claude_keys.json`` secrets to the encrypted store, once.

        Called from :func:`read` and :func:`write` so every entry point
        transparently triggers migration.

        Mapping from old flat keys to new namespaced keys:

        ====================================  ================================
        Old key (``~/claude_keys.json``)      New namespaced key
        ====================================  ================================
        ``BedrockBearerToken``                ``litellm-bedrock:api_key``
        ``ServiceSpecificCredential``         ``litellm-bedrock:api_key``
        ``DashScopeAPIKey``                   ``litellm-dashscope:api_key``
        ``DeepSeekAPIKey``                    ``litellm-deepseek:api_key``
        ``TelegramBotToken``                  ``telegram:TelegramBotToken``
        ``TelegramChatId``                    ``telegram:TelegramChatId``
        ====================================  ================================

        ``ServiceSpecificCredential`` is a nested dict whose value is at the
        key ``ServiceCredentialSecret``.  It is only used when
        ``BedrockBearerToken`` is missing or empty (preserving the old fallback
        order from the per-provider ``read_secret_key`` methods).
        """
        old_path = OLD_SECRETS_PATH
        if not old_path.is_file():
            return

        try:
            raw = old_path.read_text(encoding="utf-8")
            old_data: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            display.warning(f"{old_path} is corrupt or unreadable ({exc}); removing")
            with contextlib.suppress(OSError):
                old_path.unlink(missing_ok=True)
            return

        data = EncryptedFileStore.read_all(display)
        migrated = 0

        # --- Bedrock ---
        bedrock_new_key = "litellm-bedrock:api_key"
        bedrock_value: str = old_data.get("BedrockBearerToken", "") or ""
        if not bedrock_value:
            cred = old_data.get("ServiceSpecificCredential") or {}
            if isinstance(cred, dict):
                bedrock_value = cred.get("ServiceCredentialSecret", "") or ""
        if bedrock_value and bedrock_new_key not in data:
            data[bedrock_new_key] = bedrock_value
            migrated += 1

        # --- DashScope ---
        dashscope_value: str = old_data.get("DashScopeAPIKey", "") or ""
        if dashscope_value:
            ds_key = "litellm-dashscope:api_key"
            if ds_key not in data:
                data[ds_key] = dashscope_value
                migrated += 1

        # --- DeepSeek ---
        deepseek_value: str = old_data.get("DeepSeekAPIKey", "") or ""
        if deepseek_value:
            deepseek_key = "litellm-deepseek:api_key"
            if deepseek_key not in data:
                data[deepseek_key] = deepseek_value
                migrated += 1

        # --- Telegram ---
        tg_bot: str = old_data.get("TelegramBotToken", "") or ""
        if tg_bot:
            tg_bot_key = "telegram:TelegramBotToken"
            if tg_bot_key not in data:
                data[tg_bot_key] = tg_bot
                migrated += 1

        tg_chat: str = old_data.get("TelegramChatId", "") or ""
        if tg_chat:
            tg_chat_key = "telegram:TelegramChatId"
            if tg_chat_key not in data:
                data[tg_chat_key] = tg_chat
                migrated += 1

        if migrated:
            EncryptedFileStore.write_all(data, display=display)
            display.success(
                f"Migrated {migrated} secret(s) from ~/claude_keys.json to the secrets store"
            )

        try:
            old_path.unlink(missing_ok=True)
        except OSError as exc:
            display.warning(f"could not remove {old_path} after migration ({exc})")

    @staticmethod
    def read_all(display: DisplayService) -> dict[str, str]:
        """
        Decrypt and return all stored secrets.

        Returns an empty dict when the file doesn't exist, cannot be decrypted
        (key changed / tampering), or contains invalid JSON.
        """
        path = SECRETS_ENCRYPTED_FILE_PATH
        if not path.is_file():
            return {}

        try:
            ciphertext = path.read_bytes()
        except OSError:
            return {}

        key = KeyDerivation.derive_key(display)
        plaintext = EncryptionPrimitives.decrypt(ciphertext, key)
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
        return {str(k): v for k, v in data.items() if isinstance(v, str)}

    @staticmethod
    def write_all(data: dict[str, str], *, display: DisplayService) -> None:
        """
        Encrypt *data* and atomically write it to the secrets file.

        Uses a sibling temp file + rename for atomicity; the parent directory
        is created if missing.
        """
        path = SECRETS_ENCRYPTED_FILE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        key = KeyDerivation.derive_key(display)

        plaintext = json.dumps(data, indent=2).encode()
        ciphertext = EncryptionPrimitives.encrypt(plaintext, key)

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
