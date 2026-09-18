# This file has been created with the assistance of an AI tool.
"""Data models for the config domain."""

from typing import NamedTuple


class RegistryFingerprint(NamedTuple):
    """
    A cheap summary of the project registry, for HTTP cache validation.

    Stands in for the ``(mtime, size)`` the logs viewer used to stat off the registry
    file. ``last_change`` is unix nanoseconds, or ``None`` when nothing is registered.
    """

    last_change: int | None
    count: int
