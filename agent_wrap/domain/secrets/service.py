# This file has been edited with the assistance of an AI tool.
"""Secrets domain service — encrypted storage and sidecar-aware orchestration."""

import sys
from typing import TYPE_CHECKING

from agent_wrap.constants import TELEGRAM_SIDECAR_NAME
from agent_wrap.domain.secrets.models import SecretsCheckReport, SecretsSetResult
from agent_wrap.domain.secrets.store import EncryptedFileStore
from agent_wrap.exceptions import ProviderNotFoundError, SecretNotFoundError

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.providers.service import ProviderService
    from agent_wrap.domain.sidecars.service import SidecarService


class SecretsService:
    """Encrypted secrets store with sidecar-aware orchestration."""

    def __init__(
        self,
        provider_service: ProviderService,
        sidecar_service: SidecarService,
        display_service: DisplayService,
    ) -> None:
        self._provider_service = provider_service
        self._sidecar_service = sidecar_service
        self._display = display_service

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
        EncryptedFileStore.maybe_migrate_old_fallback(display=self._display)
        data = EncryptedFileStore.read_all(display=self._display)
        value = data.get(key)
        if value is not None:
            return value

        if not prompt_on_missing:
            raise SecretNotFoundError(key, description)

        entered = self._display.prompt_secret(description)
        data[key] = entered
        EncryptedFileStore.write_all(data, display=self._display)
        return entered

    def _write(self, key: str, description: str) -> None:
        """Prompt the user for *key* and persist it to the encrypted store."""
        EncryptedFileStore.maybe_migrate_old_fallback(display=self._display)
        entered = self._display.prompt_secret(description)
        data = EncryptedFileStore.read_all(display=self._display)
        data[key] = entered
        EncryptedFileStore.write_all(data, display=self._display)

    def _delete(self, key: str) -> None:
        """Remove *key* from the encrypted store.  No-op when absent."""
        EncryptedFileStore.maybe_migrate_old_fallback(display=self._display)
        data = EncryptedFileStore.read_all(display=self._display)
        if key in data:
            del data[key]
            EncryptedFileStore.write_all(data, display=self._display)

    def _list_keys(self) -> list[str]:
        """Return all key names currently stored (sorted)."""
        EncryptedFileStore.maybe_migrate_old_fallback(display=self._display)
        return sorted(EncryptedFileStore.read_all(display=self._display).keys())

    # -- Sidecar discovery --------------------------------------------------

    def known_sidecars(self) -> list[str]:
        """Return the sorted list of known sidecar names."""
        names = list(self._provider_service.discover_providers().keys())
        names.append(TELEGRAM_SIDECAR_NAME)
        return sorted(names)

    # -- Required secrets resolution ----------------------------------------

    def get_required_secrets(self, sidecar_name: str) -> list[tuple[str, str]]:
        """Return the required-secret ``(key, description)`` tuples for a sidecar."""
        if sidecar_name == TELEGRAM_SIDECAR_NAME:
            return self._sidecar_service.telegram_required_secrets()

        try:
            provider = self._provider_service.get_provider(sidecar_name)
        except ProviderNotFoundError:
            known = ", ".join(self.known_sidecars())
            self._display.error(f"Unknown sidecar: {sidecar_name}  (known: {known})")
            raise SystemExit(1) from None

        return provider.required_secrets()

    def _get_required_secrets_safe(self, sidecar_name: str) -> list[tuple[str, str]]:
        """
        Like :meth:`get_required_secrets` but returns empty on unknown sidecars.

        ``SystemExit`` is caught alongside ``ProviderNotFoundError`` because
        :meth:`get_required_secrets` converts the latter into the former on its way out —
        so catching only ``ProviderNotFoundError`` here would never fire, and an
        unresolvable provider would abort every caller of this "safe" variant.
        """
        try:
            return self.get_required_secrets(sidecar_name)
        except ProviderNotFoundError, SystemExit:
            return []

    # -- Sidecar secret actions ---------------------------------------------

    def check_secrets(self, sidecar_name: str) -> SecretsCheckReport:
        """
        Verify all required secrets for *sidecar_name* are present.

        Returns each namespaced key's presence together with the overall verdict, so a
        caller renders the rows rather than deciding pass/fail itself.
        """
        required = self.get_required_secrets(sidecar_name)
        entries: dict[str, bool] = {}
        for key, desc in required:
            namespaced = f"{sidecar_name}:{key}"
            try:
                self.read(namespaced, desc, prompt_on_missing=False)
                entries[namespaced] = True
            except SecretNotFoundError:
                entries[namespaced] = False
        return SecretsCheckReport(
            entries=entries,
            all_present=all(entries.values()),
            declares_none=not required,
        )

    def missing_keys_by_sidecar(self) -> dict[str, list[str]]:
        """
        Report, per known sidecar, which required secrets are absent — changing nothing.

        The read-only counterpart to :meth:`check_secrets`, which cannot be used for
        reporting on two counts: it goes through :meth:`read`, which runs the legacy
        ``~/claude_keys.json`` migration (rewriting the store and deleting that file),
        and through :meth:`get_required_secrets`, which writes to stderr and raises
        ``SystemExit`` on an unknown sidecar — so one stale name would abort a report.

        Every known sidecar appears in the result; an empty list means fully configured.
        Covers all sidecars in one call because the store is decrypted once for the whole
        sweep: per-sidecar calls would re-derive the key and re-emit any decryption
        warning once per provider.

        Only key *names* are returned, never values — the same thing
        ``agent secrets check`` prints.
        """
        stored = EncryptedFileStore.read_all(display=self._display)
        result: dict[str, list[str]] = {}
        for name in self.known_sidecars():
            required = self._get_required_secrets_safe(name)
            result[name] = sorted(
                f"{name}:{key}" for key, _desc in required if f"{name}:{key}" not in stored
            )
        return result

    def set_secrets(self, sidecar_name: str) -> SecretsSetResult:
        """
        Prompt and persist all required secrets for *sidecar_name*.

        Returns the namespaced keys that were set. A missing TTY comes back as the
        result's ``error`` rather than an exception, so the caller reports it instead
        of stringifying a ``RuntimeError``.
        """
        if not sys.stdin.isatty():
            return SecretsSetResult(
                keys_set=[], error="Cannot prompt for secrets in a non-interactive session."
            )

        required = self.get_required_secrets(sidecar_name)
        keys_set: list[str] = []
        for key, desc in required:
            namespaced = f"{sidecar_name}:{key}"
            self._write(namespaced, desc)
            keys_set.append(namespaced)
        return SecretsSetResult(keys_set=keys_set)

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
        for name in self.known_sidecars():
            required = self._get_required_secrets_safe(name)
            for key, _desc in required:
                known_keys.add(f"{name}:{key}")

        removed: list[str] = []
        for key in self._list_keys():
            if key not in known_keys:
                self._delete(key)
                removed.append(key)
        return removed
