# This file has been edited with the assistance of an AI tool.
"""Central exception classes for agent-wrap."""


class SecretNotFoundError(Exception):
    def __init__(self, key: str, description: str) -> None:
        self.key = key
        self.description = description
        super().__init__(f"Secret '{key}' ({description}) not found in secrets store")


class LockTimeoutError(RuntimeError): ...


class PortUnavailableError(RuntimeError): ...


class ProviderNotFoundError(Exception): ...


class HostMountError(Exception): ...


class DockerfileDirectiveError(Exception): ...


class StartupScriptError(Exception):
    """Raised when a project's startup script fails, times out, or cannot be executed."""


class StorageError(Exception):
    """
    Raised when a database operation fails, wrapping the underlying ``sqlite3`` error.

    Exists so no layer above ``agent_wrap/infrastructure/`` has to name sqlite3's
    exception tree. That matters on the registry write path, which must never fail a
    launch: ``sqlite3.OperationalError`` (a locked database) is not an ``OSError``, so
    the ``except OSError`` guards there would sail straight past it.
    """


class MigrationError(StorageError): ...


class WritesNotEnabledError(StorageError):
    """
    Raised when ``ConnectionFactory.rw()`` is entered without a write grant.

    A ``StorageError`` deliberately, and that is the mechanism rather than a
    convenience: every best-effort registry guard already catches ``StorageError``, so a
    process that never asked for write permission degrades to "no registry" instead of
    crashing. It is what keeps the logs daemon from mutating host state through a read.
    """
