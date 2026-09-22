# This file has been edited with the assistance of an AI tool.
"""Tests for agent_wrap.domain.secrets (encrypted-file backend)."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from unittest.mock import Mock

    import pytest_mock

import base64
import contextlib
import json
from pathlib import Path
from typing import Any

import pytest

import agent_wrap.domain.secrets.store as store_mod
from agent_wrap.domain.providers.service import ProviderService
from agent_wrap.domain.secrets.models import SecretEntry
from agent_wrap.domain.secrets.service import SecretsService
from agent_wrap.domain.secrets.store import EncryptedFileStore, KeyDerivation
from agent_wrap.domain.sidecars.service import SidecarService
from agent_wrap.exceptions import ProviderNotFoundError, SecretNotFoundError

_FIXED_KEY = b"0" * 32  # stable key for reproducible tests

#: A plausible credential and the same one pasted twice, which is what the guard undoes.
_SINGLE_SECRET = "sk-1234567890abcdef"
_DOUBLED_SECRET = _SINGLE_SECRET * 2

#: What both pinned payloads below encrypt, under ``_FIXED_KEY``. 110 bytes of JSON:
#: three full 32-byte CTR blocks plus a short tail, so the superseded reader's block
#: counter and its trailing-chunk handling are both covered.
_PINNED_SECRETS = {"litellm-bedrock:api_key": "A" * 40, "telegram:bot_token": "t" * 7}

#: A Fernet token over ``_PINNED_SECRETS``, written by the build that introduced it.
_PINNED_TOKEN = (
    b"gAAAAABlU_EAAAECAwQFBgcICQoLDA0OD9ayf5TOeeH6ln7m4hIzmb8l22gpqnADlzTsQmyvgHlI"
    b"WVErqVZX-4RhSXPTS6r5bMAsq0Qnr3hGcm9faMI3G2IGJODvmVbzh5kLZDnGG_MCxTCpmcKV-vFR"
    b"RdYsBck6Pbioe5j5R5VpZd-sXqFoUqu_Z7KYqRK-zzI3VJ8U7xzQGs8xDuZvMi1GGmw0wcaaHA=="
)

#: The same secrets in the pre-0.11.0 ``nonce || HMAC-CTR || mac`` format, which
#: :func:`store._decrypt_legacy` still reads and ``read_all`` upgrades in place.
_PINNED_LEGACY_PAYLOAD = base64.b64decode(
    "AAECAwQFBgcICQoLDA0OD6rizJ6aNa4FPhUUni7Z0uj/cHpOgTbMb6AbtrCc/pX5+ARBcWNNk+vo"
    "3/xcjY1IuO3ECcyH8NUUoHCjmOGgs/3QTsZEn7P8zF11i4UnpRdARMwfqDWCUYh2NQYC6odpPjY0"
    "ta0D0+xZ7WAdRsxe6mDsIBz7Ap0xMVt6bwBaG4Vk3PPGWCetA7gWhIWUCNw="
)
_PATCH_KEYFILE_PATH = "agent_wrap.domain.secrets.store.SECRETS_KEYFILE_PATH"
_PATCH_ENCRYPTED_FILE_PATH = "agent_wrap.domain.secrets.store.SECRETS_ENCRYPTED_FILE_PATH"
_PATCH_DERIVE_KEY = "agent_wrap.domain.secrets.store.KeyDerivation.derive_key"


def test_secret_not_found_error_repr() -> None:
    err = SecretNotFoundError("ns:key", "some description")
    assert err.key == "ns:key"
    assert err.description == "some description"
    assert "ns:key" in str(err)
    assert "some description" in str(err)


@pytest.fixture
def secrets_paths(tmp_path: Path, mocker: pytest_mock.MockerFixture) -> tuple[Path, Path]:
    """Point path constants into *tmp_path*."""
    secrets_path = tmp_path / "secrets.enc"
    keyfile_path = tmp_path / ".secrets-key"
    mocker.patch(_PATCH_ENCRYPTED_FILE_PATH, secrets_path)
    mocker.patch(_PATCH_KEYFILE_PATH, keyfile_path)
    KeyDerivation.derive_key.cache_clear()
    return secrets_path, keyfile_path


@pytest.fixture
def fixed_key(secrets_paths: tuple[Any, ...], mocker: pytest_mock.MockerFixture) -> None:  # noqa: ARG001
    """Make _derive_key always return _FIXED_KEY."""
    KeyDerivation.derive_key.cache_clear()
    mocker.patch(_PATCH_DERIVE_KEY, autospec=True, return_value=_FIXED_KEY)


@pytest.fixture
def svc(
    secrets_paths: tuple[Any, ...],  # noqa: ARG001
    fixed_key: None,  # noqa: ARG001
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> SecretsService:
    """Return a SecretsService wired to temporary paths with a fixed key."""
    return SecretsService(
        provider_service=mocker.Mock(spec=ProviderService),
        sidecar_service=mocker.Mock(spec=SidecarService),
        display_service=display_mock,
    )


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


@pytest.mark.parametrize(
    "value",
    [
        "",
        "hello",
        "x" * 5000,
        "café-\U0001f4a1\U0001f511",
    ],
)
@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_write_read_roundtrip(value: str, display_mock: Mock) -> None:
    EncryptedFileStore.write_all({"ns:key": value}, display=display_mock)
    assert EncryptedFileStore.read_all(display=display_mock) == {"ns:key": value}


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_a_pinned_token_still_decrypts(tmp_path: Path, display_mock: Mock) -> None:
    """
    Pin a token written by an earlier build against ``_FIXED_KEY``.

    Everything between the master key and the token -- the HKDF label, the base64
    armouring, the cipher itself -- is invisible to a round-trip test, which re-derives
    both halves from the same code. Change any of it and every secrets file already on
    disk becomes undecryptable while the round-trip stays green. Only a pinned token
    written by the *previous* code catches that.
    """
    (tmp_path / "secrets.enc").write_bytes(_PINNED_TOKEN)

    assert EncryptedFileStore.read_all(display=display_mock) == _PINNED_SECRETS
    display_mock.warning.assert_not_called()


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_two_writes_of_one_value_differ(tmp_path: Path, display_mock: Mock) -> None:
    """A fresh IV per write, so a repeated secret is not a repeated file."""
    secrets_path = tmp_path / "secrets.enc"
    EncryptedFileStore.write_all({"ns:key": "same"}, display=display_mock)
    first = secrets_path.read_bytes()
    EncryptedFileStore.write_all({"ns:key": "same"}, display=display_mock)

    assert secrets_path.read_bytes() != first
    assert EncryptedFileStore.read_all(display=display_mock) == {"ns:key": "same"}


#: Ways a secrets file can be damaged, each paired with the name it gets in the test id.
_CORRUPTIONS: list[tuple[Callable[[bytes], bytes], str]] = [
    (lambda t: t[:20] + bytes([t[20] ^ 0xFF]) + t[21:], "flipped ciphertext byte"),
    (lambda t: t[:-1] + bytes([t[-1] ^ 0xFF]), "flipped tag byte"),
    (lambda t: t[:20], "truncated"),
    (lambda _: b"short", "not a token at all"),
]


@pytest.mark.parametrize(("corrupt", "description"), _CORRUPTIONS)
@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_a_tampered_file_reads_as_empty(
    corrupt: Callable[[bytes], bytes],
    description: str,  # noqa: ARG001 -- names the case in the test id
    tmp_path: Path,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"ns:key": "v"}, display=display_mock)
    secrets_path = tmp_path / "secrets.enc"
    secrets_path.write_bytes(corrupt(secrets_path.read_bytes()))

    assert EncryptedFileStore.read_all(display=display_mock) == {}
    assert "decrypt" in display_mock.warning.call_args[0][0].lower()


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_the_superseded_payload_is_read_and_rewritten(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, display_mock: Mock
) -> None:
    """A pre-0.11.0 HMAC-CTR file reads back, and is upgraded in place on that read."""
    secrets_path = tmp_path / "secrets.enc"
    secrets_path.write_bytes(_PINNED_LEGACY_PAYLOAD)

    assert EncryptedFileStore.read_all(display=display_mock) == _PINNED_SECRETS
    display_mock.warning.assert_not_called()

    assert secrets_path.read_bytes() != _PINNED_LEGACY_PAYLOAD
    # The upgrade is the point: a second read must not reach the superseded reader.
    spy = mocker.spy(store_mod, "_decrypt_legacy")
    assert EncryptedFileStore.read_all(display=display_mock) == _PINNED_SECRETS
    spy.assert_not_called()


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_a_tampered_superseded_payload_reads_as_empty(tmp_path: Path, display_mock: Mock) -> None:
    secrets_path = tmp_path / "secrets.enc"
    tampered = bytearray(_PINNED_LEGACY_PAYLOAD)
    tampered[20] ^= 0xFF
    secrets_path.write_bytes(bytes(tampered))

    assert EncryptedFileStore.read_all(display=display_mock) == {}
    assert "decrypt" in display_mock.warning.call_args[0][0].lower()
    assert secrets_path.read_bytes() == bytes(tampered)


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_read_all_empty_when_no_file(
    display_mock: Mock,
) -> None:
    assert EncryptedFileStore.read_all(display=display_mock) == {}


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_read_all_returns_stored_data(
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"a": "1", "b": "2"}, display=display_mock)
    assert EncryptedFileStore.read_all(display=display_mock) == {"a": "1", "b": "2"}


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_read_all_corrupt_file(
    tmp_path: Path,
    display_mock: Mock,
) -> None:
    secrets_path = tmp_path / "secrets.enc"
    secrets_path.write_bytes(b"not valid encrypted data")
    result = EncryptedFileStore.read_all(display=display_mock)
    assert result == {}
    display_mock.warning.assert_called_once()
    assert "decrypt" in display_mock.warning.call_args[0][0].lower()


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_read_all_wrong_key_returns_empty(
    display_mock: Mock,
) -> None:
    """When the encryption key changes, _read_all warns and returns {}."""
    EncryptedFileStore.write_all({"key": "val"}, display=display_mock)

    # Change the key — derive_key is already patched (autospec) by the
    # fixed_key fixture, so update its return value rather than re-patching.
    KeyDerivation.derive_key.return_value = b"x" * 32  # pyrefly: ignore [missing-attribute]

    result = EncryptedFileStore.read_all(display=display_mock)
    assert result == {}
    display_mock.warning.assert_called_once()
    assert "decrypt" in display_mock.warning.call_args[0][0].lower()


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_write_all_creates_parent_dir(
    tmp_path: Path, mocker: pytest_mock.MockerFixture, display_mock: Mock
) -> None:
    nested = tmp_path / "sub" / "nested"
    mocker.patch(_PATCH_ENCRYPTED_FILE_PATH, nested / "secrets.enc")
    EncryptedFileStore.write_all({"x": "y"}, display=display_mock)
    assert (nested / "secrets.enc").is_file()


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_write_all_overwrites(
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"old": "data"}, display=display_mock)
    EncryptedFileStore.write_all({"new": "value"}, display=display_mock)
    assert EncryptedFileStore.read_all(display=display_mock) == {"new": "value"}


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_write_all_atomic_no_partial_read(
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


def test_read_found(svc: SecretsService, display_mock: Mock) -> None:
    EncryptedFileStore.write_all({"ns:key": "stored-value"}, display=display_mock)
    assert svc.read("ns:key", "desc") == "stored-value"


def test_read_missing_no_prompt(svc: SecretsService) -> None:
    with pytest.raises(SecretNotFoundError) as exc:
        svc.read("ns:missing", "desc", prompt_on_missing=False)
    assert exc.value.key == "ns:missing"


def test_read_missing_with_prompt(
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    display_mock.prompt_secret.return_value = "entered"

    result = svc.read("ns:new", "desc", prompt_on_missing=True)
    assert result == "entered"
    # Verify it was persisted
    assert EncryptedFileStore.read_all(display=display_mock)["ns:new"] == "entered"


def test_read_prompt_eof_error(svc: SecretsService, display_mock: Mock) -> None:
    display_mock.prompt_secret.side_effect = SystemExit

    with pytest.raises(SystemExit):
        svc.read("ns:key", "desc", prompt_on_missing=True)


def test_write_stores(svc: SecretsService, display_mock: Mock) -> None:
    display_mock.prompt_secret.return_value = "typed"

    svc._write("ns:key", "desc")
    assert EncryptedFileStore.read_all(display=display_mock)["ns:key"] == "typed"


def test_write_preserves_other_keys(
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"existing": "keep-me"}, display=display_mock)
    display_mock.prompt_secret.return_value = "new-val"

    svc._write("ns:new", "desc")
    data = EncryptedFileStore.read_all(display=display_mock)
    assert data["existing"] == "keep-me"
    assert data["ns:new"] == "new-val"


def test_delete_removes_key(
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"a": "1", "b": "2"}, display=display_mock)
    svc._delete("a")
    assert EncryptedFileStore.read_all(display=display_mock) == {"b": "2"}


def test_delete_missing_is_noop(
    svc: SecretsService,
    display_mock: Mock,
) -> None:
    EncryptedFileStore.write_all({"a": "1"}, display=display_mock)
    svc._delete("nonexistent")  # no-op
    assert EncryptedFileStore.read_all(display=display_mock) == {"a": "1"}


def test_list_keys_returns_sorted(svc: SecretsService, display_mock: Mock) -> None:
    EncryptedFileStore.write_all({"c": "3", "a": "1", "b": "2"}, display=display_mock)
    assert svc._list_keys() == ["a", "b", "c"]


def test_list_keys_empty_store(svc: SecretsService) -> None:
    assert svc._list_keys() == []


@pytest.mark.usefixtures("secrets_paths", "fixed_key")
def test_read_all_filters_non_strings(
    tmp_path: Path,
    display_mock: Mock,
) -> None:
    """_read_all skips values that are not strings (e.g. int, None)."""
    # Bypass the public API to write a dict with a non-string value
    encrypted = tmp_path / "secrets.enc"
    plaintext = json.dumps({"keep": "val", "drop_int": 42, "drop_none": None}).encode()
    encrypted.write_bytes(store_mod._fernet(_FIXED_KEY).encrypt(plaintext))

    data = EncryptedFileStore.read_all(display=display_mock)
    assert data == {"keep": "val"}
    assert "drop_int" not in data
    assert "drop_none" not in data


@pytest.fixture
def reporting_svc(
    secrets_paths: tuple[Any, ...],  # noqa: ARG001
    fixed_key: None,  # noqa: ARG001
    mocker: pytest_mock.MockerFixture,
    display_mock: Mock,
) -> SecretsService:
    """Return a SecretsService whose provider requires exactly one key."""
    provider = mocker.Mock(spec=ProviderService)
    provider.discover_providers.return_value = {"litellm-bedrock": object()}
    provider.get_provider.return_value.required_secrets.return_value = [("api_key", "API key")]

    sidecar = mocker.Mock(spec=SidecarService)
    sidecar.telegram_required_secrets.return_value = [("TelegramBotToken", "bot token")]

    return SecretsService(
        provider_service=provider,
        sidecar_service=sidecar,
        display_service=display_mock,
    )


def test_missing_keys_lists_every_known_sidecar(reporting_svc: SecretsService) -> None:
    assert set(reporting_svc.missing_keys_by_sidecar()) == {"litellm-bedrock", "telegram"}


def test_missing_keys_reports_absent_keys(reporting_svc: SecretsService) -> None:
    result = reporting_svc.missing_keys_by_sidecar()
    assert result["litellm-bedrock"] == ["litellm-bedrock:api_key"]
    assert result["telegram"] == ["telegram:TelegramBotToken"]


def test_missing_keys_empty_when_stored(reporting_svc: SecretsService, display_mock: Mock) -> None:
    EncryptedFileStore.write_all(
        {"litellm-bedrock:api_key": "v", "telegram:TelegramBotToken": "v"},
        display=display_mock,
    )
    result = reporting_svc.missing_keys_by_sidecar()
    assert result["litellm-bedrock"] == []
    assert result["telegram"] == []


def test_missing_keys_does_not_rewrite_the_store(
    reporting_svc: SecretsService, display_mock: Mock, secrets_paths: tuple[Any, ...]
) -> None:
    secrets_path, _keyfile = secrets_paths
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": "v"}, display=display_mock)
    before = secrets_path.read_bytes()

    reporting_svc.missing_keys_by_sidecar()

    assert secrets_path.read_bytes() == before


def test_missing_keys_returns_no_values(reporting_svc: SecretsService, display_mock: Mock) -> None:
    """Only key names may be reported — never the secret values themselves."""
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": "super-secret"}, display=display_mock)
    result = reporting_svc.missing_keys_by_sidecar()
    assert "super-secret" not in json.dumps(result)


def test_missing_keys_survives_unknown_provider(
    mocker: pytest_mock.MockerFixture,
    secrets_paths: tuple[Any, ...],  # noqa: ARG001
    fixed_key: None,  # noqa: ARG001
    display_mock: Mock,
) -> None:
    """A provider that cannot be resolved must not abort the whole sweep."""
    provider = mocker.Mock(spec=ProviderService)
    provider.discover_providers.return_value = {"broken": object()}
    provider.get_provider.side_effect = ProviderNotFoundError("gone")
    sidecar = mocker.Mock(spec=SidecarService)
    no_secrets: list[tuple[str, str]] = []
    sidecar.telegram_required_secrets.return_value = no_secrets
    svc = SecretsService(
        provider_service=provider, sidecar_service=sidecar, display_service=display_mock
    )

    result = svc.missing_keys_by_sidecar()

    assert result == {"broken": [], "telegram": []}


def test_prompt_does_not_confirm_a_non_repeated_secret(
    svc: SecretsService, display_mock: Mock
) -> None:
    display_mock.prompt_secret.return_value = "sk-live-unique-value"

    svc._write("ns:key", "desc")

    assert EncryptedFileStore.read_all(display=display_mock)["ns:key"] == "sk-live-unique-value"
    display_mock.prompt_confirm.assert_not_called()


def test_prompt_does_not_confirm_an_empty_entry(svc: SecretsService, display_mock: Mock) -> None:
    """The empty string has no repeating unit to offer."""
    display_mock.prompt_secret.return_value = ""

    svc._write("ns:key", "desc")

    display_mock.prompt_confirm.assert_not_called()


def test_write_stores_the_deduplicated_unit_when_accepted(
    svc: SecretsService, display_mock: Mock
) -> None:
    display_mock.prompt_secret.return_value = _DOUBLED_SECRET
    display_mock.prompt_confirm.return_value = True

    svc._write("ns:key", "desc")

    assert EncryptedFileStore.read_all(display=display_mock)["ns:key"] == _SINGLE_SECRET


def test_write_stores_the_value_as_entered_when_declined(
    svc: SecretsService, display_mock: Mock
) -> None:
    display_mock.prompt_secret.return_value = _DOUBLED_SECRET
    display_mock.prompt_confirm.return_value = False

    svc._write("ns:key", "desc")

    assert EncryptedFileStore.read_all(display=display_mock)["ns:key"] == _DOUBLED_SECRET


def test_read_stores_the_deduplicated_unit_when_accepted(
    svc: SecretsService, display_mock: Mock
) -> None:
    """The first-launch prompt goes through the same guard as 'agent secrets set'."""
    display_mock.prompt_secret.return_value = _DOUBLED_SECRET
    display_mock.prompt_confirm.return_value = True

    result = svc.read("ns:new", "desc", prompt_on_missing=True)

    assert result == _SINGLE_SECRET
    assert EncryptedFileStore.read_all(display=display_mock)["ns:new"] == _SINGLE_SECRET


def test_dedup_confirmation_states_both_lengths_without_the_value(
    svc: SecretsService, display_mock: Mock
) -> None:
    """The caution must be readable over someone's shoulder without leaking the secret."""
    display_mock.prompt_secret.return_value = _DOUBLED_SECRET
    display_mock.prompt_confirm.return_value = False

    svc._write("ns:key", "desc")

    caution = display_mock.alert.call_args[0][0]
    assert _SINGLE_SECRET not in caution
    assert f"{len(_DOUBLED_SECRET)} chars" in caution
    assert f"{len(_SINGLE_SECRET)} chars" in caution
    assert "2 copies" in caution


def test_check_reports_length_and_hint_for_a_present_key(
    reporting_svc: SecretsService, display_mock: Mock
) -> None:
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": _SINGLE_SECRET}, display=display_mock)

    report = reporting_svc.check_secrets("litellm-bedrock")

    entry = report.entries["litellm-bedrock:api_key"]
    assert entry.present is True
    assert entry.length == len(_SINGLE_SECRET)
    assert entry.hint == "sk***ef"
    assert report.all_present is True


def test_check_reports_an_absent_key_as_not_present(reporting_svc: SecretsService) -> None:
    """A NamedTuple is always truthy, so the verdict must read the field, not the entry."""
    report = reporting_svc.check_secrets("litellm-bedrock")

    entry = report.entries["litellm-bedrock:api_key"]
    assert entry.present is False
    assert entry.length == 0
    assert entry.hint == ""
    assert report.all_present is False


def test_check_reports_an_empty_stored_value_as_present(
    reporting_svc: SecretsService, display_mock: Mock
) -> None:
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": ""}, display=display_mock)

    report = reporting_svc.check_secrets("litellm-bedrock")

    assert report.entries["litellm-bedrock:api_key"] == SecretEntry(
        present=True, length=0, hint="***"
    )


def test_check_declares_none_for_a_sidecar_requiring_nothing(
    reporting_svc: SecretsService, mocker: pytest_mock.MockerFixture
) -> None:
    provider = mocker.Mock(spec=ProviderService)
    provider.discover_providers.return_value = {"litellm-bedrock": object()}
    no_secrets: list[tuple[str, str]] = []
    provider.get_provider.return_value.required_secrets.return_value = no_secrets
    reporting_svc._provider_service = provider

    report = reporting_svc.check_secrets("litellm-bedrock")

    assert report.declares_none is True
    assert report.entries == {}


def test_check_decrypts_the_store_once(
    reporting_svc: SecretsService, mocker: pytest_mock.MockerFixture
) -> None:
    """Reading per key would re-derive the master key and re-warn once per secret."""
    provider = mocker.Mock(spec=ProviderService)
    provider.discover_providers.return_value = {"litellm-bedrock": object()}
    provider.get_provider.return_value.required_secrets.return_value = [
        ("api_key", "API key"),
        ("region", "region"),
    ]
    reporting_svc._provider_service = provider
    spy = mocker.spy(store_mod.EncryptedFileStore, "read_all")

    reporting_svc.check_secrets("litellm-bedrock")

    assert spy.call_count == 1


def test_check_all_covers_every_known_sidecar(reporting_svc: SecretsService) -> None:
    """The sweep's order is the table's print order, so it is the service's to state."""
    assert list(reporting_svc.check_all_sidecars()) == ["litellm-bedrock", "telegram"]


def test_check_all_reports_each_sidecars_own_presence(
    reporting_svc: SecretsService, display_mock: Mock
) -> None:
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": _SINGLE_SECRET}, display=display_mock)

    reports = reporting_svc.check_all_sidecars()

    assert reports["litellm-bedrock"].all_present is True
    assert reports["litellm-bedrock"].entries["litellm-bedrock:api_key"] == SecretEntry(
        present=True, length=len(_SINGLE_SECRET), hint="sk***ef"
    )
    assert reports["telegram"].all_present is False
    assert reports["telegram"].entries["telegram:TelegramBotToken"] == SecretEntry(present=False)


def test_check_all_keeps_keys_namespaced(reporting_svc: SecretsService) -> None:
    """Splitting the namespace into two columns is the caller's; the service reports keys."""
    assert set(reporting_svc.check_all_sidecars()["telegram"].entries) == {
        "telegram:TelegramBotToken"
    }


def test_check_all_decrypts_the_store_once(
    reporting_svc: SecretsService, mocker: pytest_mock.MockerFixture
) -> None:
    """Per-sidecar calls would re-derive the key and re-warn once per provider."""
    spy = mocker.spy(store_mod.EncryptedFileStore, "read_all")

    reporting_svc.check_all_sidecars()

    assert spy.call_count == 1


def test_check_all_marks_a_sidecar_that_requires_nothing(
    reporting_svc: SecretsService, mocker: pytest_mock.MockerFixture
) -> None:
    provider = mocker.Mock(spec=ProviderService)
    provider.discover_providers.return_value = {"litellm-bedrock": object()}
    no_secrets: list[tuple[str, str]] = []
    provider.get_provider.return_value.required_secrets.return_value = no_secrets
    reporting_svc._provider_service = provider

    reports = reporting_svc.check_all_sidecars()

    assert reports["litellm-bedrock"].declares_none is True
    assert reports["litellm-bedrock"].entries == {}
    assert reports["litellm-bedrock"].all_present is True
    assert "telegram" in reports


def test_check_all_survives_an_unknown_provider(
    mocker: pytest_mock.MockerFixture,
    secrets_paths: tuple[Any, ...],  # noqa: ARG001
    fixed_key: None,  # noqa: ARG001
    display_mock: Mock,
) -> None:
    """A provider that cannot be resolved must not abort the sweep."""
    provider = mocker.Mock(spec=ProviderService)
    provider.discover_providers.return_value = {"broken": object()}
    provider.get_provider.side_effect = ProviderNotFoundError("gone")
    sidecar = mocker.Mock(spec=SidecarService)
    sidecar.telegram_required_secrets.return_value = [("TelegramBotToken", "bot token")]
    svc = SecretsService(
        provider_service=provider, sidecar_service=sidecar, display_service=display_mock
    )

    reports = svc.check_all_sidecars()

    assert reports["broken"].declares_none is True
    assert reports["broken"].entries == {}
    assert list(reports["telegram"].entries) == ["telegram:TelegramBotToken"]


def test_check_all_does_not_rewrite_the_store(
    reporting_svc: SecretsService, display_mock: Mock, secrets_paths: tuple[Any, ...]
) -> None:
    secrets_path, _keyfile = secrets_paths
    EncryptedFileStore.write_all({"litellm-bedrock:api_key": "v"}, display=display_mock)
    before = secrets_path.read_bytes()

    reporting_svc.check_all_sidecars()

    assert secrets_path.read_bytes() == before
