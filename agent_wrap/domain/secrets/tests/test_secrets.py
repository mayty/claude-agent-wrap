# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.secrets (encrypted-file backend)."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from unittest.mock import Mock

import contextlib
import json
from pathlib import Path
from typing import Any

import pytest
import pytest_mock

from agent_wrap.domain.secrets.service import SecretsService
from agent_wrap.domain.secrets.store import (
    EncryptedFileStore,
    EncryptionPrimitives,
    KeyDerivation,
)
from agent_wrap.exceptions import SecretNotFoundError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXED_KEY = b"0" * 32  # stable key for reproducible tests
_PATCH_KEYFILE_PATH = "agent_wrap.domain.secrets.store.SECRETS_KEYFILE_PATH"
_PATCH_ENCRYPTED_FILE_PATH = "agent_wrap.domain.secrets.store.SECRETS_ENCRYPTED_FILE_PATH"
_PATCH_OLD_SECRETS_PATH = "agent_wrap.domain.secrets.store.OLD_SECRETS_PATH"
_PATCH_DERIVE_KEY = "agent_wrap.domain.secrets.store.KeyDerivation.derive_key"


# ---------------------------------------------------------------------------
# SecretNotFoundError
# ---------------------------------------------------------------------------


def test_secret_not_found_error_repr() -> None:
    err = SecretNotFoundError("ns:key", "some description")
    assert err.key == "ns:key"
    assert err.description == "some description"
    assert "ns:key" in str(err)
    assert "some description" in str(err)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------


@pytest.fixture
def secrets_paths(tmp_path: Path, mocker: pytest_mock.MockerFixture) -> tuple[Path, Path]:
    """Point path constants into *tmp_path*."""
    secrets_path = tmp_path / "secrets.enc"
    keyfile_path = tmp_path / ".secrets-key"
    mocker.patch(_PATCH_ENCRYPTED_FILE_PATH, secrets_path)
    mocker.patch(_PATCH_KEYFILE_PATH, keyfile_path)
    mocker.patch(_PATCH_OLD_SECRETS_PATH, tmp_path / "claude_keys.json")
    KeyDerivation.derive_key.cache_clear()
    EncryptedFileStore.maybe_migrate_old_fallback.cache_clear()
    return secrets_path, keyfile_path


@pytest.fixture
def fixed_key(secrets_paths: tuple[Any, ...], mocker: pytest_mock.MockerFixture) -> None:
    """Make _derive_key always return _FIXED_KEY."""
    KeyDerivation.derive_key.cache_clear()
    mocker.patch(_PATCH_DERIVE_KEY, return_value=_FIXED_KEY)


@pytest.fixture
def svc(
    secrets_paths: tuple[Any, ...],
    fixed_key: None,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> SecretsService:
    """Return a SecretsService wired to temporary paths with a fixed key."""
    return SecretsService(
        provider_service=mocker.Mock(),
        sidecar_service=mocker.Mock(),
        display_service=display_mock,
    )


# _derive_key
# ---------------------------------------------------------------------------


def test_derive_key_stable(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, display_mock: Mock
) -> None:
    """Same inputs → same key (repeatable)."""
    KeyDerivation.derive_key.cache_clear()
    keyfile = tmp_path / ".secrets-key"
    keyfile.write_bytes(b"a" * 32)
    mocker.patch(_PATCH_KEYFILE_PATH, keyfile)
    mocker.patch("agent_wrap.domain.secrets.store.Path.read_text", return_value="fake-machine-id")
    # Mock subprocess for git

    class _FakeResult:
        returncode = 0
        stdout = "abc123def456\n"

    mocker.patch("agent_wrap.domain.secrets.store.subprocess.run", return_value=_FakeResult())

    k1 = KeyDerivation.derive_key(display=display_mock)
    KeyDerivation.derive_key.cache_clear()
    k2 = KeyDerivation.derive_key(display=display_mock)
    assert k1 == k2
    assert len(k1) == 32


def test_derive_key_creates_keyfile(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, display_mock: Mock
) -> None:
    """Keyfile is generated on first use when missing."""
    KeyDerivation.derive_key.cache_clear()
    keyfile = tmp_path / ".secrets-key"
    mocker.patch(_PATCH_KEYFILE_PATH, keyfile)
    mocker.patch("agent_wrap.domain.secrets.store.Path.read_text", return_value="mid")

    class _FakeResult:
        returncode = 0
        stdout = "abc\n"

    mocker.patch("agent_wrap.domain.secrets.store.subprocess.run", return_value=_FakeResult())

    assert not keyfile.is_file()
    KeyDerivation.derive_key(display=display_mock)
    assert keyfile.is_file()
    assert len(keyfile.read_bytes()) == 32
    # Permission check: must be owner-only (0o600 minus any umask stripping)
    perms = keyfile.stat().st_mode & 0o777
    assert perms == 0o600, f"expected 0o600, got {perms:#o}"


def test_derive_key_empty_machine_id(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, display_mock: Mock
) -> None:
    """Warns when /etc/machine-id exists but is empty."""
    KeyDerivation.derive_key.cache_clear()
    keyfile = tmp_path / ".secrets-key"
    keyfile.write_bytes(b"b" * 32)
    mocker.patch(_PATCH_KEYFILE_PATH, keyfile)
    # Return an empty machine-id
    mocker.patch(
        "agent_wrap.domain.secrets.store.Path.read_text",
        return_value="   \n",
    )

    class _FakeResult:
        returncode = 0
        stdout = "abc\n"

    mocker.patch("agent_wrap.domain.secrets.store.subprocess.run", return_value=_FakeResult())

    key = KeyDerivation.derive_key(display=display_mock)
    assert len(key) == 32
    display_mock.warning.assert_called_once()
    assert "empty" in display_mock.warning.call_args[0][0].lower()


# ---------------------------------------------------------------------------
# _encrypt / _decrypt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plaintext", "label"),
    [
        (b"", "empty"),
        (b"hello", "short"),
        (b"x" * 5000, "long"),
        ("café-\U0001f4a1\U0001f511".encode(), "unicode"),
    ],
)
def test_encrypt_decrypt_roundtrip(plaintext: bytes, label: str) -> None:
    key = b"k" * 32
    ct = EncryptionPrimitives.encrypt(plaintext, key)
    assert EncryptionPrimitives.decrypt(ct, key) == plaintext


def test_encrypt_nonce_randomness() -> None:
    """Two encryptions of the same plaintext produce different ciphertexts."""
    key = b"k" * 32
    pt = b"same data"
    ct1 = EncryptionPrimitives.encrypt(pt, key)
    ct2 = EncryptionPrimitives.encrypt(pt, key)
    assert ct1 != ct2
    # Both should decrypt to the same plaintext
    assert EncryptionPrimitives.decrypt(ct1, key) == pt
    assert EncryptionPrimitives.decrypt(ct2, key) == pt


def test_decrypt_wrong_key() -> None:
    ct = EncryptionPrimitives.encrypt(b"secret", b"a" * 32)
    assert EncryptionPrimitives.decrypt(ct, b"b" * 32) is None


def test_decrypt_tampered_ciphertext() -> None:
    ct = bytearray(EncryptionPrimitives.encrypt(b"secret", b"k" * 32))
    ct[20] ^= 0xFF  # flip bits in ciphertext
    assert EncryptionPrimitives.decrypt(bytes(ct), b"k" * 32) is None


def test_decrypt_tampered_mac() -> None:
    ct = bytearray(EncryptionPrimitives.encrypt(b"secret", b"k" * 32))
    ct[-1] ^= 0xFF  # flip last byte of MAC
    assert EncryptionPrimitives.decrypt(bytes(ct), b"k" * 32) is None


def test_decrypt_truncated() -> None:
    ct = EncryptionPrimitives.encrypt(b"secret", b"k" * 32)
    assert EncryptionPrimitives.decrypt(ct[:20], b"k" * 32) is None


def test_decrypt_too_short() -> None:
    assert EncryptionPrimitives.decrypt(b"short", b"k" * 32) is None


# ---------------------------------------------------------------------------
# _read_all / _write_all
# ---------------------------------------------------------------------------


def test_read_all_empty_when_no_file(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    assert EncryptedFileStore.read_all(display=display_mock) == {}


def test_read_all_returns_stored_data(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"a": "1", "b": "2"}, display=display_mock)
    assert EncryptedFileStore.read_all(display=display_mock) == {"a": "1", "b": "2"}


def test_read_all_corrupt_file(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    secrets_path = tmp_path / "secrets.enc"
    secrets_path.write_bytes(b"not valid encrypted data")
    result = EncryptedFileStore.read_all(display=display_mock)
    assert result == {}
    display_mock.warning.assert_called_once()
    assert "decrypt" in display_mock.warning.call_args[0][0].lower()


def test_read_all_wrong_key_returns_empty(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """When the encryption key changes, _read_all warns and returns {}."""
    EncryptedFileStore.write_all({"key": "val"}, display=display_mock)

    # Change the key
    KeyDerivation.derive_key.cache_clear()
    mocker.patch(_PATCH_DERIVE_KEY, return_value=b"x" * 32)

    result = EncryptedFileStore.read_all(display=display_mock)
    assert result == {}
    display_mock.warning.assert_called_once()
    assert "decrypt" in display_mock.warning.call_args[0][0].lower()


def test_write_all_creates_parent_dir(
    svc: SecretsService, tmp_path: Path, mocker: pytest_mock.MockerFixture, display_mock: Mock
) -> None:
    nested = tmp_path / "sub" / "nested"
    mocker.patch(_PATCH_ENCRYPTED_FILE_PATH, nested / "secrets.enc")
    EncryptedFileStore.write_all({"x": "y"}, display=display_mock)
    assert (nested / "secrets.enc").is_file()


def test_write_all_overwrites(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"old": "data"}, display=display_mock)
    EncryptedFileStore.write_all({"new": "value"}, display=display_mock)
    assert EncryptedFileStore.read_all(display=display_mock) == {"new": "value"}


def test_write_all_atomic_no_partial_read(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """A failed write doesn't corrupt existing data."""
    EncryptedFileStore.write_all({"good": "data"}, display=display_mock)
    secrets_path = tmp_path / "secrets.enc"
    original = secrets_path.read_bytes()
    # Simulate a failure during write by temporarily pointing path to a
    # read-only directory — the write will fail but the original file
    # must remain intact.
    mocker.patch(_PATCH_ENCRYPTED_FILE_PATH, Path("/nonexistent/ro/secrets.enc"))
    with contextlib.suppress(OSError, PermissionError):
        EncryptedFileStore.write_all({"bad": "write"}, display=display_mock)
    # Restore and check original is intact
    mocker.patch(_PATCH_ENCRYPTED_FILE_PATH, secrets_path)
    assert secrets_path.read_bytes() == original
    assert EncryptedFileStore.read_all(display=display_mock) == {"good": "data"}


# ---------------------------------------------------------------------------
# read() API
# ---------------------------------------------------------------------------


def test_read_found(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService, display_mock: Mock
) -> None:
    EncryptedFileStore.write_all({"ns:key": "stored-value"}, display=display_mock)
    assert svc.read("ns:key", "desc") == "stored-value"


def test_read_missing_no_prompt(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService
) -> None:
    with pytest.raises(SecretNotFoundError) as exc:
        svc.read("ns:missing", "desc", prompt_on_missing=False)
    assert exc.value.key == "ns:missing"


def test_read_missing_with_prompt(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    display_mock.prompt_secret.return_value = "entered"

    result = svc.read("ns:new", "desc", prompt_on_missing=True)
    assert result == "entered"
    # Verify it was persisted
    assert EncryptedFileStore.read_all(display=display_mock)["ns:new"] == "entered"


def test_read_prompt_eof_error(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService, display_mock: Mock
) -> None:
    display_mock.prompt_secret.side_effect = SystemExit

    with pytest.raises(SystemExit):
        svc.read("ns:key", "desc", prompt_on_missing=True)


# ---------------------------------------------------------------------------
# write() API
# ---------------------------------------------------------------------------


def test_write_stores(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService, display_mock: Mock
) -> None:
    display_mock.prompt_secret.return_value = "typed"

    svc._write("ns:key", "desc")
    assert EncryptedFileStore.read_all(display=display_mock)["ns:key"] == "typed"


def test_write_preserves_other_keys(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"existing": "keep-me"}, display=display_mock)
    display_mock.prompt_secret.return_value = "new-val"

    svc._write("ns:new", "desc")
    data = EncryptedFileStore.read_all(display=display_mock)
    assert data["existing"] == "keep-me"
    assert data["ns:new"] == "new-val"


# ---------------------------------------------------------------------------
# delete() / svc._list_keys() API
# ---------------------------------------------------------------------------


def test_delete_removes_key(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"a": "1", "b": "2"}, display=display_mock)
    svc._delete("a")
    assert EncryptedFileStore.read_all(display=display_mock) == {"b": "2"}


def test_delete_missing_is_noop(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"a": "1"}, display=display_mock)
    svc._delete("nonexistent")  # no-op
    assert EncryptedFileStore.read_all(display=display_mock) == {"a": "1"}


def test_list_keys_returns_sorted(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService, display_mock: Mock
) -> None:
    EncryptedFileStore.write_all({"c": "3", "a": "1", "b": "2"}, display=display_mock)
    assert svc._list_keys() == ["a", "b", "c"]


def test_list_keys_empty_store(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService
) -> None:
    assert svc._list_keys() == []


# ---------------------------------------------------------------------------
# Non-string value filtering
# ---------------------------------------------------------------------------


def test_read_all_filters_non_strings(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """_read_all skips values that are not strings (e.g. int, None)."""
    # Bypass the public API to write a dict with a non-string value
    encrypted = tmp_path / "secrets.enc"
    plaintext = json.dumps({"keep": "val", "drop_int": 42, "drop_none": None}).encode()
    encrypted.write_bytes(EncryptionPrimitives.encrypt(plaintext, _FIXED_KEY))

    data = EncryptedFileStore.read_all(display=display_mock)
    assert data == {"keep": "val"}
    assert "drop_int" not in data
    assert "drop_none" not in data


# ---------------------------------------------------------------------------
# Migration from old ~/claude_keys.json (pre-encryption era)
# ---------------------------------------------------------------------------


def _write_old_file(tmp_path: Path, data: dict[str, Any]) -> Path:
    """Write *data* as JSON to the mocked ``claude_keys.json`` path."""
    old = tmp_path / "claude_keys.json"
    old.write_text(json.dumps(data))
    return old


def test_migrate_no_old_file(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)
    # No crash, no side effects
    assert EncryptedFileStore.read_all(display=display_mock) == {}


def test_migrate_copies_data(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    _write_old_file(tmp_path, {"BedrockBearerToken": "bedrock-val"})

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    assert EncryptedFileStore.read_all(display=display_mock) == {
        "litellm-bedrock:api_key": "bedrock-val"
    }
    assert not (tmp_path / "claude_keys.json").is_file()


def test_migrate_all_keys(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    _write_old_file(
        tmp_path,
        {
            "BedrockBearerToken": "b",
            "DashScopeAPIKey": "d",
            "DeepSeekAPIKey": "ds",
            "TelegramBotToken": "tgb",
            "TelegramChatId": "tgc",
        },
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    assert EncryptedFileStore.read_all(display=display_mock) == {
        "litellm-bedrock:api_key": "b",
        "litellm-dashscope:api_key": "d",
        "litellm-deepseek:api_key": "ds",
        "telegram:TelegramBotToken": "tgb",
        "telegram:TelegramChatId": "tgc",
    }


def test_migrate_service_specific_credential_fallback(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """ServiceSpecificCredential used when BedrockBearerToken is missing."""
    _write_old_file(
        tmp_path,
        {"ServiceSpecificCredential": {"ServiceCredentialSecret": "ssc-secret"}},
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    assert EncryptedFileStore.read_all(display=display_mock) == {
        "litellm-bedrock:api_key": "ssc-secret"
    }


def test_migrate_bedrock_bearer_takes_priority(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """BedrockBearerToken wins over ServiceSpecificCredential when both present."""
    _write_old_file(
        tmp_path,
        {
            "BedrockBearerToken": "bearer",
            "ServiceSpecificCredential": {"ServiceCredentialSecret": "ssc"},
        },
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    assert EncryptedFileStore.read_all(display=display_mock) == {
        "litellm-bedrock:api_key": "bearer"
    }


def test_migrate_per_key_no_overwrite(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """Already-present keys are not overwritten during migration."""
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": "existing"}, display=display_mock)
    _write_old_file(
        tmp_path,
        {"BedrockBearerToken": "new-val", "DashScopeAPIKey": "ds-val"},
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    data = EncryptedFileStore.read_all(display=display_mock)
    # Bedrock was already present — preserved
    assert data["litellm-bedrock:api_key"] == "existing"
    # DashScope was missing — migrated
    assert data["litellm-dashscope:api_key"] == "ds-val"


def test_migrate_does_not_overwrite_existing(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """When the encrypted store already has all keys, nothing changes."""
    EncryptedFileStore.write_all(
        {
            "litellm-bedrock:api_key": "keep-b",
            "litellm-dashscope:api_key": "keep-d",
        },
        display=display_mock,
    )
    encrypted = tmp_path / "secrets.enc"
    encrypted_bytes = encrypted.read_bytes()

    _write_old_file(
        tmp_path,
        {"BedrockBearerToken": "should-not-migrate", "DashScopeAPIKey": "also-skip"},
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    # Encrypted file unchanged
    assert encrypted.read_bytes() == encrypted_bytes
    assert EncryptedFileStore.read_all(display=display_mock) == {
        "litellm-bedrock:api_key": "keep-b",
        "litellm-dashscope:api_key": "keep-d",
    }


def test_migrate_corrupt_old_file(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    old = tmp_path / "claude_keys.json"
    old.write_text("not valid json")

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    assert not old.is_file(), "corrupt old file should be deleted"
    assert EncryptedFileStore.read_all(display=display_mock) == {}
    display_mock.warning.assert_called_once()
    assert "corrupt" in display_mock.warning.call_args[0][0].lower()


def test_migrate_empty_old_file(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    old = tmp_path / "claude_keys.json"
    old.write_text(json.dumps({}))

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    # Empty dict: no keys to migrate, but file is still removed
    assert not old.is_file()


def test_migrate_prints_message(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    _write_old_file(tmp_path, {"TelegramBotToken": "tg"})

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    display_mock.success.assert_called_once()
    assert "Migrated 1 secret" in display_mock.success.call_args[0][0]


def test_read_triggers_migration(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, svc: SecretsService
) -> None:
    old = tmp_path / "claude_keys.json"
    old.write_text(json.dumps({"TelegramBotToken": "migrated-tg"}))

    result = svc.read("telegram:TelegramBotToken", "desc")
    assert result == "migrated-tg"
    assert not old.is_file()


def test_write_triggers_migration(
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    old = tmp_path / "claude_keys.json"
    old.write_text(json.dumps({"TelegramBotToken": "migrated-tg"}))
    display_mock.prompt_secret.return_value = "new-val"

    svc._write("new-key", "desc")

    data = EncryptedFileStore.read_all(display=display_mock)
    assert data["telegram:TelegramBotToken"] == "migrated-tg"
    assert data["new-key"] == "new-val"
    assert not old.is_file()


def test_migrate_skip_empty_values(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """Empty-string or missing keys are skipped (not migrated)."""
    _write_old_file(
        tmp_path,
        {
            "BedrockBearerToken": "",
            "DashScopeAPIKey": "",
            "TelegramBotToken": "",
        },
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    # Nothing was migrated — all values were empty
    assert EncryptedFileStore.read_all(display=display_mock) == {}


def test_migrate_bedrock_empty_falls_back_to_ssc(
    svc: SecretsService,
    tmp_path: Path,
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> None:
    """Empty BedrockBearerToken falls back to ServiceSpecificCredential."""
    _write_old_file(
        tmp_path,
        {
            "BedrockBearerToken": "",
            "ServiceSpecificCredential": {"ServiceCredentialSecret": "ssc-secret"},
        },
    )

    EncryptedFileStore.maybe_migrate_old_fallback(display=display_mock)

    assert EncryptedFileStore.read_all(display=display_mock) == {
        "litellm-bedrock:api_key": "ssc-secret"
    }
