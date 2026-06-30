# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.secrets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.secrets import (
    SecretNotFoundError,
    _fallback_get,
    _fallback_set,
    _migrate_secrets,
    read,
    write,
)

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
# Fallback JSON (monkeypatch _FALLBACK_DIR)
# ---------------------------------------------------------------------------


def _set_fallback_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_wrap.secrets.AGENT_LAUNCHES_DIR", tmp_path)


def test_fallback_get_missing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_fallback_dir(tmp_path, monkeypatch)
    assert _fallback_get("nonexistent") is None


def test_fallback_get_existing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_fallback_dir(tmp_path, monkeypatch)
    _fallback_set("test:key", "my-secret")
    assert _fallback_get("test:key") == "my-secret"


def test_fallback_get_empty_string_is_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_fallback_dir(tmp_path, monkeypatch)
    _fallback_set("test:key", "")
    assert _fallback_get("test:key") == ""


def test_fallback_set_corrupt_json_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _set_fallback_dir(tmp_path, monkeypatch)
    (tmp_path / "secrets.json").write_text("not json")
    _fallback_set("test:key", "new-value")
    assert _fallback_get("test:key") == "new-value"


def test_fallback_file_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _set_fallback_dir(tmp_path, monkeypatch)
    _fallback_set("test:key", "val")
    fallback = tmp_path / "secrets.json"
    st_mode = fallback.stat().st_mode
    assert st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# read() — with mocked backends
# ---------------------------------------------------------------------------

_PATCH_KEYRING = "agent_wrap.secrets._keyring_available"
_PATCH_FALLBACK_GET = "agent_wrap.secrets._fallback_get"


def test_read_found(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=False)
    mocker.patch(_PATCH_FALLBACK_GET, return_value="stored")
    assert read("ns:key", "desc") == "stored"


def test_read_missing_no_prompt(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=False)
    mocker.patch(_PATCH_FALLBACK_GET, return_value=None)
    with pytest.raises(SecretNotFoundError) as exc:
        read("ns:key", "desc", prompt_on_missing=False)
    assert exc.value.key == "ns:key"


def test_read_missing_with_prompt(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=False)
    mocker.patch(_PATCH_FALLBACK_GET, return_value=None)
    mock_set = mocker.patch("agent_wrap.secrets._fallback_set")
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="entered")

    result = read("ns:key", "desc", prompt_on_missing=True)
    assert result == "entered"
    mock_set.assert_called_once_with("ns:key", "entered")


def test_write_stores(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=False)
    mock_set = mocker.patch("agent_wrap.secrets._fallback_set")
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="typed")

    write("ns:key", "desc")
    mock_set.assert_called_once_with("ns:key", "typed")


_PATCH_KEYRING_GET = "agent_wrap.secrets._keyring_get"
_PATCH_KEYRING_SET = "agent_wrap.secrets._keyring_set"


def test_read_keyring_found(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=True)
    mocker.patch(_PATCH_KEYRING_GET, return_value="from-ring")
    mock_fb = mocker.patch(_PATCH_FALLBACK_GET)
    assert read("ns:key", "desc") == "from-ring"
    mock_fb.assert_not_called()


def test_read_keyring_missing_no_prompt(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=True)
    mocker.patch(_PATCH_KEYRING_GET, return_value=None)
    mocker.patch(_PATCH_FALLBACK_GET, return_value=None)
    with pytest.raises(SecretNotFoundError):
        read("ns:key", "desc", prompt_on_missing=False)


def test_read_keyring_missing_with_prompt(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=True)
    mocker.patch(_PATCH_KEYRING_GET, return_value=None)
    mock_set = mocker.patch(_PATCH_KEYRING_SET, return_value=True)
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="prompted")

    result = read("ns:key", "desc", prompt_on_missing=True)
    assert result == "prompted"
    mock_set.assert_called_once_with("ns:key", "prompted")


def test_write_keyring_path(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=True)
    mock_set = mocker.patch(_PATCH_KEYRING_SET, return_value=True)
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="typed")

    write("ns:key", "desc")
    mock_set.assert_called_once_with("ns:key", "typed")


def test_read_keyring_prompt_eof_error(mocker: pytest_mock.MockFixture) -> None:
    mocker.patch(_PATCH_KEYRING, return_value=True)
    mocker.patch(_PATCH_KEYRING_GET, return_value=None)
    mocker.patch("agent_wrap.secrets.getpass.getpass", side_effect=EOFError)

    with pytest.raises(SecretNotFoundError):
        read("ns:key", "desc", prompt_on_missing=True)


# ---------------------------------------------------------------------------
# Migration from old ~/claude_keys.json
# ---------------------------------------------------------------------------

_TEST_DIR = "agent_wrap.secrets.AGENT_LAUNCHES_DIR"
_TEST_OLD_PATH = "agent_wrap.secrets._OLD_SECRETS_PATH"


def _setup_migration_test(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old_json: dict | None,
    pre_seed: dict[str, str] | None = None,
) -> Path:
    """Set up common state for migration tests."""
    _migrate_secrets.cache_clear()
    monkeypatch.setattr("agent_wrap.secrets._keyring_available", lambda: False)
    monkeypatch.setattr(_TEST_DIR, tmp_path)
    old_path = tmp_path / "claude_keys.json"
    monkeypatch.setattr(_TEST_OLD_PATH, old_path)
    if old_json is not None:
        old_path.write_text(json.dumps(old_json))
    if pre_seed:
        for k, v in pre_seed.items():
            _fallback_set(k, v)
    return old_path


def test_migrate_no_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ~/claude_keys.json does not exist, migration is a no-op."""
    _migrate_secrets.cache_clear()
    monkeypatch.setattr("agent_wrap.secrets._keyring_available", lambda: False)
    monkeypatch.setattr(_TEST_DIR, tmp_path)
    monkeypatch.setattr(_TEST_OLD_PATH, tmp_path / "nonexistent.json")
    # Should not raise
    _migrate_secrets()


def test_migrate_corrupt_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Corrupt JSON logs warning and deletes the file."""
    old_path = _setup_migration_test(tmp_path, monkeypatch, old_json=None)
    old_path.write_text("not valid json")

    _migrate_secrets()

    captured = capsys.readouterr()
    assert "corrupt" in captured.err.lower()
    assert not old_path.is_file(), "corrupt file should be deleted"


def test_migrate_all_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """All old keys are migrated to correct new namespaced keys, file deleted."""
    old_path = _setup_migration_test(
        tmp_path,
        monkeypatch,
        old_json={
            "BedrockBearerToken": "bedrock-token",
            "DashScopeAPIKey": "dashscope-key",
            "DeepSeekAPIKey": "deepseek-key",
            "TelegramBotToken": "tg-bot",
            "TelegramChatId": "12345",
        },
    )

    _migrate_secrets()

    assert _fallback_get("litellm-bedrock:api_key") == "bedrock-token"
    assert _fallback_get("litellm-dashscope:api_key") == "dashscope-key"
    assert _fallback_get("litellm-deepseek:api_key") == "deepseek-key"
    assert _fallback_get("telegram:TelegramBotToken") == "tg-bot"
    assert _fallback_get("telegram:TelegramChatId") == "12345"
    assert not old_path.is_file()
    captured = capsys.readouterr()
    assert "Migrated 5 secret(s)" in captured.err


def test_migrate_does_not_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Existing new-style secrets are preserved (not overwritten)."""
    old_path = _setup_migration_test(
        tmp_path,
        monkeypatch,
        old_json={"BedrockBearerToken": "old-value"},
        pre_seed={"litellm-bedrock:api_key": "existing-value"},
    )

    _migrate_secrets()

    assert _fallback_get("litellm-bedrock:api_key") == "existing-value"
    assert not old_path.is_file()
    captured = capsys.readouterr()
    assert "migrated 0" not in captured.err  # bedrock was skipped
    # Migration message should NOT appear (0 secrets migrated means no message,
    # but the file is still deleted)


def test_migrate_bedrock_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """ServiceSpecificCredential.ServiceCredentialSecret used when flat key missing."""
    old_path = _setup_migration_test(
        tmp_path,
        monkeypatch,
        old_json={
            "ServiceSpecificCredential": {"ServiceCredentialSecret": "legacy-secret"},
        },
    )

    _migrate_secrets()

    assert _fallback_get("litellm-bedrock:api_key") == "legacy-secret"
    assert not old_path.is_file()
    captured = capsys.readouterr()
    assert "Migrated 1 secret(s)" in captured.err


def test_migrate_bedrock_flat_over_legacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """BedrockBearerToken takes priority over ServiceSpecificCredential."""
    old_path = _setup_migration_test(
        tmp_path,
        monkeypatch,
        old_json={
            "BedrockBearerToken": "flat-token",
            "ServiceSpecificCredential": {"ServiceCredentialSecret": "legacy-secret"},
        },
    )

    _migrate_secrets()

    assert _fallback_get("litellm-bedrock:api_key") == "flat-token"
    assert not old_path.is_file()
    captured = capsys.readouterr()
    assert "Migrated 1 secret(s)" in captured.err


def test_migrate_skips_empty_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Empty string values in old file are treated as missing."""
    old_path = _setup_migration_test(
        tmp_path,
        monkeypatch,
        old_json={
            "BedrockBearerToken": "",
            "DashScopeAPIKey": "",
            "DeepSeekAPIKey": "deepseek-key",
            "TelegramBotToken": "",
            "TelegramChatId": "",
        },
    )

    _migrate_secrets()

    assert _fallback_get("litellm-deepseek:api_key") == "deepseek-key"
    assert _fallback_get("litellm-bedrock:api_key") is None
    assert _fallback_get("litellm-dashscope:api_key") is None
    assert _fallback_get("telegram:TelegramBotToken") is None
    assert _fallback_get("telegram:TelegramChatId") is None
    assert not old_path.is_file()
    captured = capsys.readouterr()
    assert "Migrated 1 secret(s)" in captured.err


def test_migrate_bedrock_legacy_not_a_dict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ServiceSpecificCredential that is not a dict is gracefully skipped."""
    _ = _setup_migration_test(
        tmp_path,
        monkeypatch,
        old_json={"ServiceSpecificCredential": "not-a-dict"},
    )

    _migrate_secrets()

    assert _fallback_get("litellm-bedrock:api_key") is None


def test_read_triggers_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """Calling read() triggers migration before lookup."""
    _migrate_secrets.cache_clear()
    monkeypatch.setattr("agent_wrap.secrets._keyring_available", lambda: False)
    monkeypatch.setattr(_TEST_DIR, tmp_path)
    old_path = tmp_path / "claude_keys.json"
    old_path.write_text(json.dumps({"TelegramBotToken": "migrated-tg"}))
    monkeypatch.setattr(_TEST_OLD_PATH, old_path)

    result = read("telegram:TelegramBotToken", "desc")

    assert result == "migrated-tg"
    assert not old_path.is_file()


def test_write_triggers_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mocker: pytest_mock.MockFixture
) -> None:
    """Calling write() triggers migration before prompting."""
    _migrate_secrets.cache_clear()
    monkeypatch.setattr("agent_wrap.secrets._keyring_available", lambda: False)
    monkeypatch.setattr(_TEST_DIR, tmp_path)
    old_path = tmp_path / "claude_keys.json"
    old_path.write_text(json.dumps({"TelegramBotToken": "migrated-tg"}))
    monkeypatch.setattr(_TEST_OLD_PATH, old_path)
    mocker.patch("agent_wrap.secrets.getpass.getpass", return_value="new-value")

    write("telegram:TelegramBotToken", "desc")

    assert _fallback_get("telegram:TelegramBotToken") == "new-value"
    assert not old_path.is_file()
