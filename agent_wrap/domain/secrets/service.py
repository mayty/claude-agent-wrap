# This file has been created with the assistance of an AI tool.
"""Secrets domain service — encrypted storage and sidecar-aware orchestration."""

from __future__ import annotations

import getpass
import sys
from typing import TYPE_CHECKING

from agent_wrap.domain.secrets.store import EncryptedFileStore
from agent_wrap.exceptions import ProviderNotFoundError, SecretNotFoundError

if TYPE_CHECKING:
    from agent_wrap.domain.providers.service import ProviderService
    from agent_wrap.domain.sidecars.service import SidecarService


class SecretsService:
    """Encrypted secrets store with sidecar-aware orchestration."""

    def __init__(
        self,
        provider_service: ProviderService,
        sidecar_service: SidecarService,
    ) -> None:
        self._provider_service = provider_service
        self._sidecar_service = sidecar_service

    # -- Core CRUD ----------------------------------------------------------

    def read(self, key: str, description: str, *, prompt_on_missing: bool = False) -> str:
        """
        Return the secret for *key*.

        Looks up *key* in the encrypted secrets store.  If not found and
        *prompt_on_missing* is ``True`` the user is interactively prompted and the
        entered value is persisted before it is returned.

        Raises :class:`SecretNotFoundError` when the key is absent and
        *prompt_on_missing* is ``False``.
        """
        EncryptedFileStore.maybe_migrate_old_fallback()
        data = EncryptedFileStore.read_all()
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
        EncryptedFileStore.write_all(data)
        return entered

    def _write(self, key: str, description: str) -> None:
        """Prompt the user for *key* and persist it to the encrypted store."""
        EncryptedFileStore.maybe_migrate_old_fallback()
        print(f"Secret: {description}", file=sys.stderr)
        entered = getpass.getpass("Value: ")
        data = EncryptedFileStore.read_all()
        data[key] = entered
        EncryptedFileStore.write_all(data)

    def _delete(self, key: str) -> None:
        """Remove *key* from the encrypted store.  No-op when absent."""
        EncryptedFileStore.maybe_migrate_old_fallback()
        data = EncryptedFileStore.read_all()
        if key in data:
            del data[key]
            EncryptedFileStore.write_all(data)

    def _list_keys(self) -> list[str]:
        """Return all key names currently stored (sorted)."""
        EncryptedFileStore.maybe_migrate_old_fallback()
        return sorted(EncryptedFileStore.read_all().keys())

    # -- Sidecar discovery --------------------------------------------------

    def _known_sidecars(self) -> list[str]:
        """Return the sorted list of known sidecar names."""
        names = list(self._provider_service.discover_providers().keys())
        names.append("telegram")
        return sorted(names)

    # -- Required secrets resolution ----------------------------------------

    def get_required_secrets(self, sidecar_name: str) -> list[tuple[str, str]]:
        """Return the required-secret ``(key, description)`` tuples for a sidecar."""
        if sidecar_name == "telegram":
            return self._sidecar_service.telegram_required_secrets()

        try:
            provider = self._provider_service.get_provider(sidecar_name)
        except ProviderNotFoundError:
            known = ", ".join(self._known_sidecars())
            print(
                f"Unknown sidecar: {sidecar_name}  (known: {known})",
                file=sys.stderr,
            )
            raise SystemExit(1) from None

        return provider.required_secrets()

    def _get_required_secrets_safe(self, sidecar_name: str) -> list[tuple[str, str]]:
        """Like :meth:`get_required_secrets` but returns empty on unknown sidecars."""
        try:
            return self.get_required_secrets(sidecar_name)
        except ProviderNotFoundError:
            return []

    # -- Sidecar secret actions ---------------------------------------------

    def check_secrets(self, sidecar_name: str) -> dict[str, bool]:
        """
        Verify all required secrets for *sidecar_name* are present.

        Returns a dict mapping each namespaced key to ``True`` (present)
        or ``False`` (missing).
        """
        required = self.get_required_secrets(sidecar_name)
        result: dict[str, bool] = {}
        for key, desc in required:
            namespaced = f"{sidecar_name}:{key}"
            try:
                self.read(namespaced, desc, prompt_on_missing=False)
                result[namespaced] = True
            except SecretNotFoundError:
                result[namespaced] = False
        return result

    def set_secrets(self, sidecar_name: str) -> list[str]:
        """
        Prompt and persist all required secrets for *sidecar_name*.

        Returns the list of namespaced keys that were set.

        Raises :class:`RuntimeError` when stdin is not a TTY.
        """
        if not sys.stdin.isatty():
            msg = "Cannot prompt for secrets in a non-interactive session."
            raise RuntimeError(msg)

        required = self.get_required_secrets(sidecar_name)
        keys_set: list[str] = []
        for key, desc in required:
            namespaced = f"{sidecar_name}:{key}"
            self._write(namespaced, desc)
            keys_set.append(namespaced)
        return keys_set

    def clear_secrets(self, sidecar_name: str) -> list[str]:
        """Delete all secrets for *sidecar_name*. Returns the list of removed keys."""
        prefix = f"{sidecar_name}:"
        removed: list[str] = []
        for key in self._list_keys():
            if key.startswith(prefix):
                self._delete(key)
                removed.append(key)
        return removed

    def cleanup_secrets(self) -> list[str]:
        """
        Remove all keys not belonging to any known sidecar/provider.

        Returns the list of removed keys.
        """
        known_keys: set[str] = set()
        for name in self._known_sidecars():
            required = self._get_required_secrets_safe(name)
            for key, _desc in required:
                known_keys.add(f"{name}:{key}")

        removed: list[str] = []
        for key in self._list_keys():
            if key not in known_keys:
                self._delete(key)
                removed.append(key)
        return removed
