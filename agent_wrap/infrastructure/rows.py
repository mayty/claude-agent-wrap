# This file has been created with the assistance of an AI tool.
"""Turning a ``sqlite3.Row`` into the NamedTuple a repository returns."""

from typing import TYPE_CHECKING, Any

from agent_wrap.infrastructure.models import RowModel

if TYPE_CHECKING:
    import sqlite3


def row_to[T: RowModel](model: type[T], row: sqlite3.Row, **overrides: Any) -> T:
    """
    Build *model* from *row*, taking each field from the column of the same name.

    Every repository read here selects its columns under the field names the app object
    uses, so the mapping is the identity for all but a handful of fields. *overrides*
    supply those: a nested key object, a decoded JSON list, a column whose name differs,
    or a value that came from a second statement. A field named in *overrides* is never
    looked up in *row*, so a model field with no column behind it is legal.

    What this gives up is per-field checking at the construction site -- a column the
    schema renamed is a ``KeyError`` on read rather than a type error. The schema is
    pinned by numbered migrations and every repository is covered by a test that reads
    what it wrote, so that error surfaces at the same moment either way.
    """
    fields: tuple[str, ...] = model._fields
    values: dict[str, Any] = {name: row[name] for name in fields if name not in overrides}
    return model(**values, **overrides)
