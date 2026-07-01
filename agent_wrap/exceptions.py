# This file has been created with the assistance of an AI tool.
"""Central exception classes for agent-wrap."""

from __future__ import annotations


class SecretNotFoundError(Exception):
    """Raised when a required secret is not found in the store."""

    def __init__(self, key: str, description: str) -> None:
        self.key = key
        self.description = description
        super().__init__(f"Secret '{key}' ({description}) not found in secrets store")


class LockTimeoutError(RuntimeError):
    """Raised by :func:`file_lock` when the lock can't be acquired in time."""


class ProviderNotFoundError(Exception):
    """Raised when a requested provider name is not found in the registry."""
