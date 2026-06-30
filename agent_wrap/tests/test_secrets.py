# This file has been created with the assistance of an AI tool.
"""Tests for agent_wrap.secrets."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_mock

from agent_wrap.secrets import (
    SecretNotFoundError,
    _fallback_get,
    _fallback_set,
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
