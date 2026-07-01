# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.secrets (encrypted-file backend)."""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.secrets import (
    SecretNotFoundError,
    _decrypt,
    _derive_key,
    _encrypt,
    _maybe_migrate_old_fallback,
    _read_all,
    _write_all,
    delete,
    list_keys,
    read,
    write,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FIXED_KEY = b"0" * 32  # stable key for reproducible tests
_PATCH_SECRETS_PATH = "agent_wrap.secrets._secrets_path"
_PATCH_KEYFILE_PATH = "agent_wrap.secrets._keyfile_path"
_PATCH_DERIVE_KEY = "agent_wrap.secrets._derive_key"
_PATCH_AGENT_LAUNCHES = "agent_wrap.secrets.AGENT_LAUNCHES_DIR"


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


def _setup_temp_paths(tmp_path: Path, mocker: pytest_mock.MockFixture) -> tuple[Path, Path]:
    """Point secrets_path and keyfile_path into *tmp_path*."""
    secrets_path = tmp_path / "secrets.enc"
    keyfile_path = tmp_path / ".secrets-key"
    mocker.patch(_PATCH_SECRETS_PATH, return_value=secrets_path)
    mocker.patch(_PATCH_KEYFILE_PATH, return_value=keyfile_path)
    mocker.patch(_PATCH_AGENT_LAUNCHES, tmp_path)
    _derive_key.cache_clear()
    return secrets_path, keyfile_path


def _setup_fixed_key(mocker: pytest_mock.MockFixture) -> None:
    """Make _derive_key always return _FIXED_KEY."""
    _derive_key.cache_clear()
    mocker.patch(_PATCH_DERIVE_KEY, return_value=_FIXED_KEY)


# ---------------------------------------------------------------------------
# _derive_key
# ---------------------------------------------------------------------------


def test_derive_key_stable(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Same inputs → same key (repeatable)."""
    _derive_key.cache_clear()
    keyfile = tmp_path / ".secrets-key"
    keyfile.write_bytes(b"a" * 32)
    mocker.patch(_PATCH_KEYFILE_PATH, return_value=keyfile)
    mocker.patch("agent_wrap.secrets.Path.read_text", return_value="fake-machine-id")
    # Mock subprocess for git
    import subprocess

    class _FakeResult:
        returncode = 0
        stdout = "abc123def456\n"

    mocker.patch.object(subprocess, "run", return_value=_FakeResult())

    k1 = _derive_key()
    _derive_key.cache_clear()
    k2 = _derive_key()
    assert k1 == k2
    assert len(k1) == 32


def test_derive_key_creates_keyfile(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Keyfile is generated on first use when missing."""
    _derive_key.cache_clear()
    keyfile = tmp_path / ".secrets-key"
    mocker.patch(_PATCH_KEYFILE_PATH, return_value=keyfile)
    mocker.patch("agent_wrap.secrets.Path.read_text", return_value="mid")
    import subprocess

    class _FakeResult:
        returncode = 0
        stdout = "abc\n"

    mocker.patch.object(subprocess, "run", return_value=_FakeResult())

    assert not keyfile.is_file()
    _derive_key()
    assert keyfile.is_file()
    assert len(keyfile.read_bytes()) == 32
    # Permission check: must be owner-only (0o600 minus any umask stripping)
    perms = keyfile.stat().st_mode & 0o777
    assert perms == 0o600, f"expected 0o600, got {perms:#o}"


def test_derive_key_empty_machine_id(
    tmp_path: Path, mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture
) -> None:
    """Warns when /etc/machine-id exists but is empty."""
    _derive_key.cache_clear()
    keyfile = tmp_path / ".secrets-key"
    keyfile.write_bytes(b"b" * 32)
    mocker.patch(_PATCH_KEYFILE_PATH, return_value=keyfile)
    # Return an empty machine-id
    mocker.patch(
        "agent_wrap.secrets.Path.read_text",
        return_value="   \n",
    )
    import subprocess

    class _FakeResult:
        returncode = 0
        stdout = "abc\n"

    mocker.patch.object(subprocess, "run", return_value=_FakeResult())

    key = _derive_key()
    assert len(key) == 32
    captured = capsys.readouterr()
    assert "empty" in captured.err.lower()


# ---------------------------------------------------------------------------
# _encrypt / _decrypt
# ---------------------------------------------------------------------------


def test_encrypt_decrypt_roundtrip_empty() -> None:
    key = b"k" * 32
    ct = _encrypt(b"", key)
    assert _decrypt(ct, key) == b""


def test_encrypt_decrypt_roundtrip_short() -> None:
    key = b"k" * 32
    plaintext = b"hello"
    ct = _encrypt(plaintext, key)
    assert _decrypt(ct, key) == plaintext


def test_encrypt_decrypt_roundtrip_long() -> None:
    key = b"k" * 32
    plaintext = b"x" * 5000
    ct = _encrypt(plaintext, key)
    assert _decrypt(ct, key) == plaintext


def test_encrypt_decrypt_roundtrip_unicode() -> None:
    key = b"k" * 32
    plaintext = "café-\U0001f4a1\U0001f511".encode()
    ct = _encrypt(plaintext, key)
    assert _decrypt(ct, key) == plaintext


def test_encrypt_nonce_randomness() -> None:
    """Two encryptions of the same plaintext produce different ciphertexts."""
    key = b"k" * 32
    pt = b"same data"
    ct1 = _encrypt(pt, key)
    ct2 = _encrypt(pt, key)
    assert ct1 != ct2
    # Both should decrypt to the same plaintext
    assert _decrypt(ct1, key) == pt
    assert _decrypt(ct2, key) == pt


def test_decrypt_wrong_key() -> None:
    ct = _encrypt(b"secret", b"a" * 32)
    assert _decrypt(ct, b"b" * 32) is None


def test_decrypt_tampered_ciphertext() -> None:
    ct = bytearray(_encrypt(b"secret", b"k" * 32))
    ct[20] ^= 0xFF  # flip bits in ciphertext
    assert _decrypt(bytes(ct), b"k" * 32) is None


def test_decrypt_tampered_mac() -> None:
    ct = bytearray(_encrypt(b"secret", b"k" * 32))
    ct[-1] ^= 0xFF  # flip last byte of MAC
    assert _decrypt(bytes(ct), b"k" * 32) is None


def test_decrypt_truncated() -> None:
    ct = _encrypt(b"secret", b"k" * 32)
    assert _decrypt(ct[:20], b"k" * 32) is None


def test_decrypt_too_short() -> None:
    assert _decrypt(b"short", b"k" * 32) is None


# ---------------------------------------------------------------------------
# _read_all / _write_all
# ---------------------------------------------------------------------------


def test_read_all_empty_when_no_file(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    assert _read_all() == {}


def test_read_all_returns_stored_data(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"a": "1", "b": "2"})
    assert _read_all() == {"a": "1", "b": "2"}


def test_read_all_corrupt_file(
    tmp_path: Path, mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture
) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    secrets_path = tmp_path / "secrets.enc"
    secrets_path.write_bytes(b"not valid encrypted data")
    result = _read_all()
    assert result == {}
    captured = capsys.readouterr()
    assert "decrypt" in captured.err.lower()


def test_read_all_wrong_key_returns_empty(
    tmp_path: Path, mocker: pytest_mock.MockFixture, capsys: pytest.CaptureFixture
) -> None:
    """When the encryption key changes, _read_all warns and returns {}."""
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"key": "val"})

    # Change the key
    _derive_key.cache_clear()
    mocker.patch(_PATCH_DERIVE_KEY, return_value=b"x" * 32)

    result = _read_all()
    assert result == {}
    captured = capsys.readouterr()
    assert "decrypt" in captured.err.lower()


def test_write_all_creates_parent_dir(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    nested = tmp_path / "sub" / "nested"
    mocker.patch(_PATCH_SECRETS_PATH, return_value=nested / "secrets.enc")
    mocker.patch(_PATCH_AGENT_LAUNCHES, nested)
    _setup_fixed_key(mocker)
    _write_all({"x": "y"})
    assert (nested / "secrets.enc").is_file()


def test_write_all_overwrites(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"old": "data"})
    _write_all({"new": "value"})
    assert _read_all() == {"new": "value"}


def test_write_all_atomic_no_partial_read(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """A failed write doesn't corrupt existing data."""
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"good": "data"})
    secrets_path = tmp_path / "secrets.enc"
    original = secrets_path.read_bytes()
    # Simulate a failure during write by temporarily pointing path to a
    # read-only directory — the write will fail but the original file
    # must remain intact.
    mocker.patch(_PATCH_SECRETS_PATH, return_value=Path("/nonexistent/ro/secrets.enc"))
    with contextlib.suppress(OSError, PermissionError):
        _write_all({"bad": "write"})
    # Restore and check original is intact
    mocker.patch(_PATCH_SECRETS_PATH, return_value=secrets_path)
    assert secrets_path.read_bytes() == original
    assert _read_all() == {"good": "data"}


# ---------------------------------------------------------------------------
# read() API
# ---------------------------------------------------------------------------


def test_read_found(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"ns:key": "stored-value"})
    assert read("ns:key", "desc") == "stored-value"


def test_read_missing_no_prompt(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    with pytest.raises(SecretNotFoundError) as exc:
        read("ns:missing", "desc", prompt_on_missing=False)
    assert exc.value.key == "ns:missing"


def test_read_missing_with_prompt(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="entered")

    result = read("ns:new", "desc", prompt_on_missing=True)
    assert result == "entered"
    # Verify it was persisted
    assert _read_all()["ns:new"] == "entered"


def test_read_prompt_eof_error(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    mocker.patch("agent_wrap.secrets.getpass.getpass", side_effect=EOFError)

    with pytest.raises(SecretNotFoundError):
        read("ns:key", "desc", prompt_on_missing=True)


# ---------------------------------------------------------------------------
# write() API
# ---------------------------------------------------------------------------


def test_write_stores(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="typed")

    write("ns:key", "desc")
    assert _read_all()["ns:key"] == "typed"


def test_write_preserves_other_keys(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"existing": "keep-me"})
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="new-val")

    write("ns:new", "desc")
    data = _read_all()
    assert data["existing"] == "keep-me"
    assert data["ns:new"] == "new-val"


# ---------------------------------------------------------------------------
# delete() / list_keys() API
# ---------------------------------------------------------------------------


def test_delete_removes_key(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"a": "1", "b": "2"})
    delete("a")
    assert _read_all() == {"b": "2"}


def test_delete_missing_is_noop(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"a": "1"})
    delete("nonexistent")  # no-op
    assert _read_all() == {"a": "1"}


def test_list_keys_returns_sorted(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _write_all({"c": "3", "a": "1", "b": "2"})
    assert list_keys() == ["a", "b", "c"]


def test_list_keys_empty_store(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    assert list_keys() == []


# ---------------------------------------------------------------------------
# Non-string value filtering
# ---------------------------------------------------------------------------


def test_read_all_filters_non_strings(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """_read_all skips values that are not strings (e.g. int, None)."""
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    # Bypass the public API to write a dict with a non-string value
    from agent_wrap.secrets import _encrypt, _secrets_path

    plaintext = json.dumps({"keep": "val", "drop_int": 42, "drop_none": None}).encode()
    _secrets_path().write_bytes(_encrypt(plaintext, _FIXED_KEY))

    data = _read_all()
    assert data == {"keep": "val"}
    assert "drop_int" not in data
    assert "drop_none" not in data


def test_migrate_filters_non_strings(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    """Migration skips entries with non-string values."""
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text(json.dumps({"keep": "val", "drop": 123, "also_drop": None}))

    _maybe_migrate_old_fallback()

    data = _read_all()
    assert data == {"keep": "val"}
    assert not old.is_file()


# ---------------------------------------------------------------------------
# Migration from old plaintext secrets.json
# ---------------------------------------------------------------------------


def test_migrate_no_old_file(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    _maybe_migrate_old_fallback()
    # No crash, no side effects
    assert _read_all() == {}


def test_migrate_copies_data(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text(json.dumps({"a": "1", "b": "2"}))

    _maybe_migrate_old_fallback()

    assert _read_all() == {"a": "1", "b": "2"}
    assert not old.is_file(), "old file should be deleted after migration"


def test_migrate_does_not_overwrite_existing(
    tmp_path: Path, mocker: pytest_mock.MockFixture
) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    # Pre-populate encrypted store
    _write_all({"existing": "keep"})
    encrypted = tmp_path / "secrets.enc"
    encrypted_bytes = encrypted.read_bytes()

    # Create old file
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text(json.dumps({"a": "should-not-migrate"}))

    _maybe_migrate_old_fallback()

    # Encrypted file unchanged
    assert encrypted.read_bytes() == encrypted_bytes
    assert _read_all() == {"existing": "keep"}


def test_migrate_corrupt_old_file(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text("not valid json")

    _maybe_migrate_old_fallback()

    assert not old.is_file(), "corrupt old file should be deleted"
    assert _read_all() == {}


def test_migrate_empty_old_file(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text(json.dumps({}))

    _maybe_migrate_old_fallback()

    assert not old.is_file(), "empty old file should be deleted"


def test_read_triggers_migration(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text(json.dumps({"telegram:TelegramBotToken": "migrated-tg"}))

    result = read("telegram:TelegramBotToken", "desc")
    assert result == "migrated-tg"
    assert not old.is_file()


def test_write_triggers_migration(tmp_path: Path, mocker: pytest_mock.MockFixture) -> None:
    _setup_temp_paths(tmp_path, mocker)
    _setup_fixed_key(mocker)
    old = tmp_path / "secrets.json"
    mocker.patch("agent_wrap.secrets._OLD_FALLBACK_PATH", old)
    old.write_text(json.dumps({"old-key": "old-val"}))
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="new-val")

    write("new-key", "desc")

    assert _read_all()["old-key"] == "old-val"
    assert _read_all()["new-key"] == "new-val"
    assert not old.is_file()
