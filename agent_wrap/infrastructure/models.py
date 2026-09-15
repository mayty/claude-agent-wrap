# This file has been created with the assistance of an AI tool.
"""Data models for the infrastructure layer."""

from typing import TYPE_CHECKING, ClassVar, NamedTuple, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class RowModel(Protocol):
    """
    The NamedTuple shape :func:`agent_wrap.infrastructure.rows.row_to` builds.

    ``_fields`` is NamedTuple's public API despite the underscore, and it is the only
    thing that function needs of its type argument.
    """

    _fields: ClassVar[tuple[str, ...]]


class MigrationFile(NamedTuple):
    version: int
    path: Path
